# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------

# pylint: disable=too-many-lines
import copy
import itertools
import json
import os
import re
try:
    from shlex import quote as shell_escape
except ImportError:
    from pipes import quote as shell_escape
from six.moves.urllib.parse import urljoin  # pylint: disable=import-error

from azure.cli.core.prompting import prompt
import azure.cli.core.azlogging as azlogging
import azure.cli.command_modules.batch_extensions._pool_utils as pool_utils

logger = azlogging.get_az_logger(__name__)


_ROOT_FILE_UPLOAD_URL = 'https://raw.githubusercontent.com/Azure/azure-batch-cli-extensions/master'
_FILE_EGRESS_OVERRIDE = 'FILE_EGRESS_OVERRIDE_URL'
_FILE_EGRESS_ENV_NAME = 'AZ_BATCH_FILE_UPLOAD_CONFIG'
_FILE_EGRESS_PREFIX = 'azure/cli/command_modules/batch_extensions/fileegress/'
_FILE_EGRESS_RESOURCES = {
    _FILE_EGRESS_PREFIX + 'batchfileuploader.py',
    _FILE_EGRESS_PREFIX + 'configuration.py',
    _FILE_EGRESS_PREFIX + 'requirements.txt',
    _FILE_EGRESS_PREFIX + 'setup_uploader.py',
    _FILE_EGRESS_PREFIX + 'uploader.py',
    _FILE_EGRESS_PREFIX + 'util.py',
    _FILE_EGRESS_PREFIX + 'uploadfiles.py'}
# These properties are reserved for application template use
# and may not be used on jobs using an application template
_PROPS_RESERVED_FOR_TEMPLATES = {
    'jobManagerTask',
    'jobPreparationTask',
    'jobReleaseTask',
    'commonEnvironmentSettings',
    'usesTaskDependencies',
    'onAllTasksComplete',
    'onTaskFailure',
    'taskFactory'}
_PROPS_PERMITTED_ON_TEMPLATES = _PROPS_RESERVED_FOR_TEMPLATES.union({
    'templateMetadata',
    'parameters',
    'metadata'})
# These properties are reserved for job use
# and may not be used on an application template
_PROPS_RESERVED_FOR_JOBS = {
    'id',
    'displayName',
    'priority',
    'constraints',
    'poolInfo',
    'applicationTemplateInfo'}
# Properties on a repeatTask object that should be
# applied to each expanded task.
_PROPS_ON_REPEAT_TASK = {
    'displayName',
    'resourceFiles',
    'environmentSettings',
    'constraints',
    'userIdentity',
    'exitConditions',
    'clientExtensions',
    'outputFiles',
    'packageReferences'}
_PROPS_ON_COLLECTION_TASK = _PROPS_ON_REPEAT_TASK.union({
    'multiInstanceSettings',
    'dependsOn'})


def _validate_int(value, content):
    """Return parameter value as an integer.
    :param str value: The raw parameter value.
    :param dict content: The template parameter definition.
    :returns: int
    """
    original = str(value)
    try:
        value = int(value)
    except ValueError:
        raise TypeError()
    if str(value) != original:
        raise TypeError()
    try:
        if value < int(content['minValue']):
            raise ValueError("Minimum value: {}".format(content['minValue']))
    except KeyError:
        pass
    try:
        if value > int(content['maxValue']):
            raise ValueError("Maximum value: {}".format(content['maxValue']))
    except KeyError:
        pass
    return value


def _validate_string(value, content):
    """Return parameter value as a string.
    :param str value: The raw parameter value.
    :param dict content: The template parameter definition.
    :returns: str
    """
    if value in [None, ""]:
        raise TypeError()
    value = str(value)
    try:
        if len(value) < int(content['minLength']):
            raise ValueError("Minimum length: {}".format(content['minLength']))
    except KeyError:
        pass
    try:
        if len(value) > int(content['maxLength']):
            raise ValueError("Maximum length: {}".format(content['maxLength']))
    except KeyError:
        pass
    return value


def _validate_bool(value):
    """Return parameter value as boolean.
    :param str value: The raw parameter value.
    :param dict content: The template parameter definition.
    :returns: bool
    """
    if value in [True, False]:
        return value
    if str(value).lower() == 'true':
        return True
    elif str(value).lower() == 'false':
        return False
    else:
        raise TypeError()


def _is_substitution(content, start, end):
    """This is to support non-ARM-style direct parameter string substitution as a
    simplification of the concat function. We may wish to remove this
    if we want to adhere more strictly to ARM.
    :param str content: The contents of an expression from the template.
    :param int start: The start index of the expression.
    :param int end: The end index of the expression.
    """
    return not (content[start - 1] == '"' and content[end + 1] == '"')


def _find(delimiter, content, start_index):
    """Given that a string starts at the index specified, scan for the end of that string.
    :param str delimiter: Delimiter for which to search.
    :param str content: String to scan.
    :param int start_index: Index of the character after opening string delimiter.
    :returns: Index of the closing string delimiter.
    """
    index = start_index
    while index < len(content):
        char = content[index]
        if char == '\\':
            index += 1
        elif char == delimiter:
            return index
        index += 1
    raise ValueError()


def _find_nested(delimiter, content, start_index):
    """Scan a string to find a specified delimiter, respecting nesting of brackets and strings.
    :param str delimiter: Delimiter for which to search.
    :param str content: String to scan.
    :param int start_index: Index of the character after opening string delimiter.
    :returns: Index of the closing string delimiter.
    """
    index = start_index
    while index < len(content):
        char = content[index]
        if char == delimiter:
            return index
        elif char == '[':
            index = _find_nested(']', content, index + 1)
        elif char == '(':
            index = _find_nested(')', content, index + 1)
        elif char == '"':
            index = _find('"', content, index + 1)
        elif char == '\'':
            index = _find('\'', content, index + 1)
        index += 1
    return index


def _get_output_source_url(path):
    """Determines the file URL for an OutputFiles dependency.
    :param str path: The path to the file.
    :returns: The URL to the file.
    """
    root_url = os.environ.get(_FILE_EGRESS_OVERRIDE, _ROOT_FILE_UPLOAD_URL)
    root_url = root_url if root_url.endswith('/') else root_url + '/'
    return urljoin(root_url, path)


def _merge_metadata(base_metadata, more_metadata):
    """Merge metadata from two different sources.
    :param list base_metadata: A (possibly undefined) set of metadata.
    :param list more_metadata: Metadata to add (also possible undefined).
    """
    result = []
    if base_metadata:
        result.extend(base_metadata)
    if more_metadata:
        conflicts = [k for k in [m['name'] for m in more_metadata]
                     if k in [m['name'] for m in result]]
        if conflicts:
            raise ValueError("May not have multiple definitions for metadata "
                             "value(s) '{}'".format(', '.join(conflicts)))
        else:
            result.extend(more_metadata)
    return result


def _resolve_template_file(job, working_dir):
    """Resolve the template file path relative to the working directory.
    :param dict job: A job specification.
    :returns: The resolved path string.
    """
    path = os.path.join(working_dir, job['applicationTemplateInfo']['filePath'])
    return os.path.normpath(path)


def _get_installation_cmdline(references, os_flavor):
    """Build the installation command line for package reference collection.
    :param dict references: Package installation references.
    :param str os_flavor: The pool OS flavor.
    """
    # pylint: disable=too-many-statements
    if not references:
        return
    builder = ""
    package_type = None
    type_error = 'PackageReferences may only contain a single type of package reference.'
    for reference in references:
        if not reference.get('type') or not reference.get('id'):
            raise ValueError("A PackageReference must have a 'type' and 'id' element.")
        if reference['type'] == 'aptPackage':
            if package_type and package_type != 'apt':
                raise ValueError(type_error)
            if os_flavor != pool_utils.PoolOperatingSystemFlavor.LINUX:
                raise ValueError('aptPackage is only supported when targeting Linux pools.')
            package_type = 'apt'
            apt_cmd = "=" + str(reference['version']) if reference.get('version') else ""
            apt_cmd = "apt-get install -y {}{}".format(reference['id'], apt_cmd)
            builder += ';' + apt_cmd if builder else apt_cmd
            # TODO: deal with repository, keyUrl, sourceLine
        elif reference['type'] == 'chocolateyPackage':
            if package_type and package_type != 'choco':
                raise ValueError(type_error)
            if os_flavor != pool_utils.PoolOperatingSystemFlavor.WINDOWS:
                raise ValueError(
                    'chocolateyPackage is only supported when targeting Windows pools.')
            package_type = 'choco'
            choco_cmd = ' --allow-empty-checksums' if reference.get('allowEmptyChecksums') else ""
            if reference.get('version'):
                choco_cmd = " --version {}{}".format(reference['version'], choco_cmd)
            choco_cmd = "choco install {}{}".format(reference['id'], choco_cmd)
            builder += ' & ' + choco_cmd if builder else choco_cmd
        elif reference['type'] == 'yumPackage':
            if package_type and package_type != 'yum':
                raise ValueError(type_error)
            if os_flavor != pool_utils.PoolOperatingSystemFlavor.LINUX:
                raise ValueError('yumPackage is only supported when targeting Linux pools.')
            package_type = 'yum'
            yum_cmd = ''
            if reference.get('disableExcludes'):
                yum_cmd = ' --disableexcludes=' + str(reference['disableExcludes'])
            if reference.get('version'):
                yum_cmd = '-{}{}'.format(reference['version'], yum_cmd)
            yum_cmd = 'yum -y install {}{}'.format(reference['id'], yum_cmd)
            builder += ';' + yum_cmd if builder else yum_cmd
        # TODO: deal with rpmRepository
        # rpm -Uvh <rpmRepository>
        elif reference['type'] == 'applicationPackage':
            raise ValueError("ApplicationPackage type for id '{}' is not supported "
                             "in this version.".format(reference['id']))
        else:
            raise ValueError("Unknown PackageReference type '{}' "
                             "for id '{}'.".format(reference['type'], reference['id']))
    if package_type == 'apt':
        command = 'apt-get update;' + builder
    elif package_type == 'choco':
        command = ('powershell -NoProfile -ExecutionPolicy unrestricted '
                   '-Command "(iex ((new-object net.webclient).DownloadString'
                   '(\'https://chocolatey.org/install.ps1\')))" && SET '
                   'PATH="%PATH%;%ALLUSERSPROFILE%\\chocolatey\\bin"')
        command += ' && choco feature enable -n=allowGlobalConfirmation & ' + builder
        # TODO: Do we need to double check with pool agent name
    elif package_type == 'yum':
        command = builder
    return {'cmdLine': command, 'isWindows': package_type == 'choco'}


def _validate_generated_job(job):
    """Validate the partial job generated from an application template prior
    to merging it with the original job.
    :param dict job: A partial generated job specification to validate.
    """
    # Rule: The job generated by an application template may not use properties reserved for job use
    # (This is a safety to prevent clever abuse of template syntax
    # to specify things that shouldn't be.)
    reserved = [k for k in job if k in _PROPS_RESERVED_FOR_JOBS]
    if reserved:
        raise ValueError("Application templates may not specify these "
                         "properties: {}".format(', '.join(reserved)))


def _validate_parameter_usage(parameters, definitions):
    """Validate the parameters supplied by the job against those defined on the template.
    :param dict parameters: Parameters supplied by the job.
    :param dict definitions: Parameter definitions from the application template.
    """
    if parameters is None:
        parameters = {}
    if definitions is None:
        definitions = {}
    for name, definition in definitions.items():
        # Rule: If the parameter definition has no default value, the template must provide a value
        parameter = parameters.get(name, definition.get('defaultValue'))
        if parameter is None:
            raise ValueError("A value for parameter '{}' must be provided "
                             "by the job.".format(name))
        # Rule: If the parameter definition specifies 'int', the value provided must be compatible
        if definition['type'] == 'int':
            try:
                _validate_int(parameter, {})
            except TypeError:
                raise ValueError("'Value '{}' supplied for parameter '{}' must be an "
                                 "integer.".format(parameter, name))
        # Rule: if the parameter definition specified 'bool', the value provided must be compatible
        elif definition['type'] == 'bool':
            try:
                _validate_bool(parameter)
            except TypeError:
                raise ValueError("'Value '{}' supplied for parameter '{}' must be a "
                                 "boolean.".format(parameter, name))
    # Rule: Only parameters values defined by the template are permitted
    violations = [k for k in parameters if k not in definitions]
    if violations:
        raise ValueError("Provided parameter(s) {} are not expected "
                         "by the template.".format(', '.join(violations)))


def _validate_job_requesting_app_template(job, working_dir):
    """Validate a job requesting an application template prior to template expansion.
    :param dict job: A job speficiation to be validated.
    :param str working_dir: Folder from which the original job was loaded (if any).
    """
    # Rule: If job doesn't request an application template, don't validate further.
    if job.get('applicationTemplateInfo') is None:
        return
    # Rule: Job must specify a template to use.
    if not job['applicationTemplateInfo'].get('filePath'):
        raise ValueError("No filePath specified for requested application template "
                         "(define applicationTemplateInfo.filePath and try again).")
    # Rule: Template file must exist
    # (We do this in order to give a good diagnostic in the most common case, knowing that this is
    # technically a race condition because someone could delete the file between our check here and
    # reading the file later on. We expect such cases to be rare.)
    template_filepath = _resolve_template_file(job, working_dir)
    try:
        with open(template_filepath, 'r'):
            pass
    except EnvironmentError as error:
        raise ValueError("Unable to read the template '{}': {}".format(template_filepath, error))
    # Rule: Jobs may not use properties reserved for template use
    reserved = [k for k in job if k in _PROPS_RESERVED_FOR_TEMPLATES]
    if reserved:
        raise ValueError("Jobs using application templates may not use these "
                         "properties: {}".format(', '.join(reserved)))


def _validate_application_template(template):
    """Validate an application template prior to use.
    :param dict template: The application template to validate.
    """
    # Rule: Templates may not use properties reserved for job use
    reserved = [k for k in template if k in _PROPS_RESERVED_FOR_JOBS]
    if reserved:
        raise ValueError("Application templates may not use these job "
                         "properties: {}".format(', '.join(reserved)))
    # Rule: Templates may only specify properties permitted
    unsupported = [k for k in template if k not in _PROPS_PERMITTED_ON_TEMPLATES]
    if unsupported:
        raise ValueError("Application templates may not use these "
                         "properties: {}".format(', '.join(unsupported)))
    # Rule: Every parameter declared on a template must specify one of the supported types
    supported_types = ['int', 'string', 'bool']
    if template.get('parameters'):
        for name, value in template['parameters'].items():
            try:
                if value['type'] not in supported_types:
                    raise ValueError("The parameter '{}' specifies an unsupported "
                                     "type: {}".format(name, value['type']))
            except KeyError:
                raise ValueError("The parameter '{}' does not specify a type.".format(name))


def _validate_metadata(metadata):
    """Validate the provided metadata is valid.
    :param list metadata: A list of metadata dicts.
    """
    # Rule: The prefix 'az_batch:' is reserved for our use
    # and can't be specified on job nor on template.
    violation = [k for k in [m['name'] for m in metadata] if k.startswith('az_batch')]
    if violation:
        raise ValueError("Metadata item(s) '{}' cannot be used; the prefix 'az_batch:' is "
                         "reserved for Batch use.".format(', '.join(violation)))


def _validate_parameter(name, content, value):
    """Validate the input parameter is valid for specified template. Checks the following:
        Check input fit with parameter type, if yes, convert to correct type
        Check input matched with the restriction of parameter
    :param str name: The parameter name.
    :param dict content: The template parameter definition.
    :param str value: The raw parameter value.
    :returns: Validated input paramater, otherwise None.
    """
    try:
        if content['type'] == 'int':
            value = _validate_int(value, content)
        elif content['type'] == 'bool':
            value = _validate_bool(value)
        elif content['type'] == 'string':
            value = _validate_string(value, content)  # pylint: disable=redefined-variable-type
        if value not in content.get('allowedValues', [value]):
            raise ValueError("Allowed values: {}".format(', '.join(content['allowedValues'])))
    except TypeError:
        logger.warning("The value '%s' of parameter '%s' is not a %s",
                       name, value, content['type'])
        return None
    except ValueError as value_error:
        logger.warning(
            "The value '%s' of parameter '%s' does not meet the requirement: %s",
            name, value, str(value_error))
        return None
    else:
        return value


def _get_template_params(template, param_values):
    """Return all required parameter values for the specified template.
    :param dict template: Template JSON object.
    :param dict param_values: User provided parameter values.
    """
    param_keys = {}
    try:
        for param, values in template['parameters'].items():
            if 'type' not in values:
                raise ValueError('Parameter {} does not have type defined'.format(param))
            try:
                # Support both ARM and dictionary syntax
                # ARM: '<PropertyName>' : { 'value' : '<PropertyValue>' }
                # Dictionary: '<PropertyName>' : <PropertyValue>'
                value = param_values[param]
                param_keys[param] = value.get('value') if isinstance(value, dict) else value
            except KeyError:
                param_keys[param] = values.get('defaultValue')
            while param_keys[param] is None:
                param_prompt = param
                description = values.get('metadata', {}).get('description')
                param_prompt += " ({}): ".format(description) if description else ": "
                param_keys[param] = prompt(param_prompt)
                param_keys[param] = _validate_parameter(param, values, param_keys[param])
    except KeyError:
        pass  # No parameters to expand
    return param_keys


def _parse_arm_parameter(name, template_obj, parameters):
    """Render the content of an ARM property
    :param str name: The name of the property to render.
    :param dict template_obj: The loaded contents of the JSON template.
    :param dict parameters: The loaded contents of the JSON parameters.
    """
    if 'parameters' not in template_obj:
        raise ValueError("Template defines no parameters but tried to use '{}'".format(name))
    try:
        param_def = template_obj['parameters'][name]
    except KeyError:
        raise ValueError("Template does not define parameter '{}'".format(name))
    user_value = param_def.get('defaultValue')
    if parameters and name in parameters:
        # Support both ARM and dictionary syntax
        # ARM: '<PropertyName>' : { 'value' : '<PropertyValue>' }
        # Dictionary: '<PropertyName>' : <PropertyValue>'
        user_value = parameters[name]
        try:
            user_value = user_value['value']
        except TypeError:
            pass
    if not user_value:
        raise ValueError("No value supplied for parameter '{}' and no default value".format(name))
    if isinstance(user_value, dict):
        # If substitute value is a complex object - it may require
        # additional parameter substitutions
        return _parse_template(json.dumps(user_value), template_obj, parameters)
    if param_def['type'] == 'int':
        return _validate_int(user_value, param_def)
    elif param_def['type'] == 'bool':
        return _validate_bool(user_value)
    elif param_def['type'] == 'string':
        return _validate_string(user_value, param_def)
    else:
        raise TypeError("Parameter type '{}' not supported.".format(param_def['type']))


def _parse_arm_variable(name, template_obj, parameters):
    """Render the value of an ARM variable.
    :param str name: The name of the variable to render.
    :param dict template_obj: The loaded contents of the JSON template.
    :param dict parameters: The loaded contents of the JSON parameters.
    """
    try:
        variable = _parse_arm_expression(
            template_obj['variables'][name],
            template_obj, parameters)
    except KeyError:
        raise ValueError("Template contains no definition for variable '{}'".format(name))
    if isinstance(variable, dict):
        # If substitute value is a complex object - it may require
        # additional parameter substitutions
        return _parse_template(json.dumps(variable), template_obj, parameters)
    return variable


def _parse_arm_concat(expression, template_obj, parameters):
    """Evaluate an ARM concat expression.
    :param str expression: The concat expression to evaluate.
    :param dict template_obj: The loaded contents of the JSON template.
    :param dict parameters: The loaded contents of the JSON parameters.
    """
    content = ""
    index = 0
    while index < len(expression):
        end = _find_nested(',', expression, index)
        argument = expression[index:end].strip()
        content += _parse_arm_expression(argument, template_obj, parameters)
        index = end + 1
    return content


def _parse_arm_expression(expression, template_obj, parameters):
    """Determine if a section of the template is an ARM reference, and calculate
    the replacement accordingly. The result will be correctly typed to suit the
    parameter definition (e.g. will return a number if the parameter requires a number)
    :param str expression: A section of template contained within [].
    :param dict template_obj: The loaded contents of the JSON template.
    :param dict parameters: The loaded contents of the JSON parameters.
    """
    if not isinstance(expression, str):
        return expression
    if expression[0] == '[' and expression[-1] == ']':
        # Remove the enclosing brackets to check the contents
        return _parse_arm_expression(expression[1:-1], template_obj, parameters)
    if expression[0] == '(' and expression[-1] == ')':
        # If the section is surrounded by ( ), then we need to further process the contents
        # as either a parameter name, or a concat operation
        return _parse_arm_expression(expression[1:-1], template_obj, parameters)
    if expression[0] == '\'' and expression[-1] == '\'':
        # If a string, remove quotes in order to perform parameter look-up
        return expression[1:-1]
    if re.match(r'^parameters', expression):
        result = _parse_arm_parameter(expression[12:-2], template_obj, parameters)
    elif re.match(r'^variables', expression):
        result = _parse_arm_variable(expression[11:-2], template_obj, parameters)
    elif re.match(r'^concat', expression):
        result = _parse_arm_concat(expression[7:-1], template_obj, parameters)
    elif re.match(r'^reference', expression):
        raise NotImplementedError("ARM-style 'reference' syntax not supported.")
    else:
        result = expression
    return result


def _parse_template_string(string_content, template_obj, parameters):
    """Given a string value (including quotes), evaluate any embedded template expressions
    delimited by '[' and ']'.
    :param str string_content: The contents of the template string.
    :param dict template_obj: The loaded JSON template file.
    :param dict parameters: The contents of the parameters file.
    """
    updated_content = ""
    current_index = 0
    while current_index < len(string_content):
        try:
            expression_start = string_content.index('[', current_index)
        except ValueError:  # No template expression to evaluate
            break
        if expression_start < len(string_content) - 1 and \
                string_content[expression_start + 1] == '[':
            # Found escaped expression
            updated_content += string_content[current_index:expression_start] + '['
            current_index = expression_start + 2
            continue
        expression_end = _find_nested(']', string_content, expression_start + 1)
        if expression_end >= len(string_content):
            # No closing delimiter for the expression (not our problem)
            break
        # Everything between [ and ]
        expression = string_content[expression_start + 1:expression_end]
        parsed = _parse_arm_expression(expression, template_obj, parameters)
        if _is_substitution(string_content, expression_start, expression_end):
            # Replacing within the middle of a string
            updated_content += string_content[current_index:expression_start] + str(parsed)
            current_index = expression_end + 1
        elif isinstance(parsed, bool):
            parsed = "true" if parsed else "false"
            updated_content += string_content[current_index:expression_start - 1] + parsed
            current_index = expression_end + 2
        elif isinstance(parsed, int):
            # Replacing an entire element value, and we want to remove any surrounding quotes
            updated_content += string_content[current_index:expression_start - 1] + str(parsed)
            current_index = expression_end + 2
        elif isinstance(parsed, dict):
            json_content = json.dumps(parsed)
            updated_content += string_content[current_index:expression_start - 1] + json_content
            current_index = expression_end + 2
        else:
            updated_content += string_content[current_index:expression_start] + str(parsed)
            current_index = expression_end + 1
    updated_content += string_content[current_index:]
    return updated_content


def _parse_template(template_str, template_obj, parameters):
    """Expand all parameters, and variables in the template.

    We want to expand all template expressions (delimited by '[' and ']') in the supplied template
    string. However, that syntax collides with JSON syntax for arrays and we don't want to collide
    with any of those. To avoid such a collision, we iterate through all of the string values
    (delimited by double quotes (")) and then expand template expressions only within those.

    :param str template_str: Content of the template file as a string.
    :param dict template_obj: Contents of the template file.
    :param dict parameters: Contents of the parameters file.
    :returns: Fully resolved JSON template.
    """
    updated_json = ""
    current_index = 0
    while current_index < len(template_str):
        try:
            string_start = template_str.index('"', current_index)
        except ValueError:  # Didn't find another string to expand
            break
        try:
            string_end = _find('"', template_str, string_start + 1)
        except ValueError:  # Didn't find terminating quote for string (not our problem)
            break
        string_content = template_str[string_start:string_end + 1]
        if '[' in string_content:
            updated_json += template_str[current_index:string_start]
            updated_json += _parse_template_string(string_content, template_obj, parameters)
        else:
            updated_json += template_str[current_index:string_end + 1]
        current_index = string_end + 1
    updated_json += template_str[current_index:]
    return json.loads(updated_json)


def _process_resource_files(request, fileutils):
    """Parse a request body for any references to resource files and transform
    them to API resourceFile format where applicable.
    :param dict request: Job or task specification.
    :returns: The updated job or task specification.
    """
    if isinstance(request, list):
        return [_process_resource_files(r, fileutils) for r in request]
    try:
        for parameter, value in request.items():
            if parameter in ['resourceFiles', 'commonResourceFiles'] and isinstance(value, list):
                new_resources = []
                for file_ref in value:
                    new_resources.extend(fileutils.resolve_resource_file(file_ref))
                request[parameter] = new_resources
            elif isinstance(value, dict) or isinstance(value, list):
                request[parameter] = _process_resource_files(value, fileutils)
    except AttributeError:
        # Request is not a dictionary - just return.
        pass
    return request


def _parse_task_output_files(task, os_flavor, file_utils):
    """Process a task's outputFiles section and update the task accordingly.
    :param dict task: A task specification.
    :param str os_flavor: The OS flavor of the pool.
    :returns: A new task specification with modifications.
    """
    if task.get('outputFiles') is None:
        return task
    new_task = {k: v for k, v in task.items() if k != 'outputFiles'}
    # Validate the output file configuration
    for output_file in task['outputFiles']:
        for prop in ['filePattern', 'destination', 'uploadDetails']:
            if prop not in output_file:
                raise ValueError("outputFile must include '{}'".format(prop))
        destination = output_file['destination']
        if 'container' not in destination and 'autoStorage' not in destination:
            raise ValueError("outputFile must include 'container' or 'autoStorage' property.")
        if 'container' in destination and 'autoStorage' in destination:
            raise ValueError("outputFile can not have both 'container' "
                             "and 'autoStorage' properties.")
        if 'autoStorage' in destination:
            if 'fileGroup' not in destination['autoStorage']:
                raise ValueError("'autoStorage' of 'destination' must have 'fileGroup' property.")
            destination['container'] = {'containerSas': \
                    file_utils.get_container_sas(destination['autoStorage']['fileGroup'])}
            if 'path' in destination['autoStorage']:
                destination['container']['path'] = destination['autoStorage']['path']
            destination.pop('autoStorage')
        if not output_file['uploadDetails'].get('taskStatus'):
            raise ValueError("outputFile.uploadDetails must include taskStatus.")
    # Edit the command line to run the upload
    if os_flavor == pool_utils.PoolOperatingSystemFlavor.WINDOWS:
        # TODO: Do we need windows shell escaping?
        upload_cmd = '%AZ_BATCH_JOB_PREP_WORKING_DIR%\\uploadfiles.py'
        full_upload_cmd = "{} & {} %errorlevel%".format(new_task['commandLine'], upload_cmd)
        new_task['commandLine'] = 'cmd /c "{}"'.format(full_upload_cmd)
    elif os_flavor == pool_utils.PoolOperatingSystemFlavor.LINUX:
        upload_cmd = '$AZ_BATCH_JOB_PREP_WORKING_DIR/uploadfiles.py'
        full_upload_cmd = shell_escape(
            '{};err=$?;{} $err;exit $err'.format(new_task['commandLine'], upload_cmd))
        new_task['commandLine'] = '/bin/bash -c {}'.format(full_upload_cmd)
    else:
        raise ValueError("Unknown pool OS flavor: " + os_flavor)
    config = {'outputFiles': task['outputFiles']}
    config_str = json.dumps(config)
    if new_task.get('environmentSettings') is None:
        new_task['environmentSettings'] = []
    new_task['environmentSettings'].append({'name': _FILE_EGRESS_ENV_NAME, 'value': config_str})
    return new_task


def _transform_sweep_str(data, parameters):
    """Replace string placeholders with parametric sweep values.
    :param str data: The string containing placeholders.
    :param list parameters: The sweep values, each value maps
     to one of {0}, {1}, .. {n} by index.
    """
    # Handle {n} or {n:m} scenario
    reg = re.compile(r'\{(\d+)(:(\d+))?\}')

    def replace(match):
        r, r1, _, r3 = [data[start:end] for start, end in match.regs]
        n = int(r1)
        if n >= len(parameters):
            raise ValueError("The parameter pattern '{}' is out of bound.".format(r))
        number_str = str(parameters[n])
        if ':' in r:
            # This is {n:m} scenario
            if parameters[n] < 0:
                raise ValueError(
                    "The parameter '{}' is negative and cannot be used in pattern '{}'.".format(
                        parameters[n], r))
            m = int(r3)
            if m < 1 or m > 9:
                raise ValueError(
                    "The parameter pattern '{}' is out of bound. "
                    "The padding number can be only between 1 to 9.".format(r))
            return number_str.zfill(m)
        else:
            # This is just {n} scenario
            return number_str
    return reg.sub(replace, data)


def _transform_file_str(content, file_ref):
    """Replace string with file value.
    :param str content: The string to be replaced.
    :param dict file_ref: The file information, containing 'url',
     'filePath etc properties.
    """
    replace_props = ['url', 'filePath', 'fileName', 'fileNameWithoutExtension']
    for prop in replace_props:
        content = re.sub("{" + prop + "}", file_ref[prop], content)
    return content


def _replacement_transform(transformer, source_obj, source_key, context):
    """Transform a string by applying specific context values.
    By design, user should escape all the literal '{' or '}' to '{{' or '}}'.
    All other '{' or '}' characters are used for replacement
    :param func transformer: The tranformation function to run.
    :param dict source_obj: The object containing the string to be transformed.
    :param str key: The key of the string to be transformed.
    :param context: The specific context to apply to the string.
    """
    source_str = source_obj.get(source_key)
    if not source_str:
        return {}
    # Handle '{' and '}' escape scenario : replace '{{' to LEFT_BRACKET_REPLACE_CHAR,
    # and '}}' to RIGHT_BRACKET_REPLACE_CHAR. The reverse function is used to handle {{{0}}}.
    LEFT_BRACKET_REPLACE_CHAR = u'\uE800'  # pylint: disable=anomalous-unicode-escape-in-string
    RIGHT_BRACKET_REPLACE_CHAR = u'\uE801'  # pylint: disable=anomalous-unicode-escape-in-string
    transformed = re.sub(r'\{\{', LEFT_BRACKET_REPLACE_CHAR, source_str)[::-1]
    transformed = re.sub(r'\}\}', RIGHT_BRACKET_REPLACE_CHAR, transformed)[::-1]
    transformed = transformer(transformed, context)
    if '{' in transformed or '}' in transformed:
        raise ValueError(
            "Invalid use of bracket characters, did you forget to escape (using {{}})?")
    # Replace LEFT_BRACKET_REPLACE_CHAR back to '{', and RIGHT_BRACKET_REPLACE_CHAR back to '}'
    transformed = re.sub(LEFT_BRACKET_REPLACE_CHAR, '{', transformed)
    transformed = re.sub(RIGHT_BRACKET_REPLACE_CHAR, '}', transformed)
    return {source_key: transformed}


def _transform_repeat_task(task, context, index, transformer):
    """Apply the transformer to a task template to yield a new task.
    :param dict task: The repeatTask task template.
    :param context: The task-factory specific context to apply to the template.
    :param index: The task factory index to use as task ID.
    :param func transformer: The transforming function to apply the
     context to the template.
    """
    new_task = copy.deepcopy(task)
    new_task.update(_replacement_transform(transformer, new_task, 'commandLine', context))
    new_task.update(_replacement_transform(transformer, new_task, 'displayName', context))
    for resource in new_task.get('resourceFiles', []):
        resource.update(_replacement_transform(transformer, resource, 'filePath', context))
        try:
            for param in ['fileGroup', 'prefix', 'containerUrl', 'url']:
                resource['source'].update(
                    _replacement_transform(transformer, resource['source'], param, context))
        except KeyError:
            resource.update(_replacement_transform(transformer, resource, 'blobSource', context))
    for env_variable in new_task.get('environmentSettings', []):
        for param in ['name', 'value']:
            env_variable.update(_replacement_transform(transformer, env_variable, param, context))
    for output in new_task.get('outputFiles', []):
        output.update(_replacement_transform(transformer, output, 'filePattern', context))
        try:
            for param in ['path', 'containerSas']:
                output['destination']['container'].update(
                    _replacement_transform(
                        transformer, output['destination']['container'], param, context))
        except KeyError:
            pass
        try:
            for param in ['path', 'fileGroup']:
                output['destination']['autoStorage'].update(
                    _replacement_transform(
                        transformer, output['destination']['autoStorage'], param, context))
        except KeyError:
            pass
    docker_options = new_task.get('clientExtensions', {}).get('dockerOptions', {})
    docker_options.update(_replacement_transform(transformer, docker_options, 'image', context))
    for volume in docker_options.get('dataVolumes', []):
        for param in ['hostPath', 'containerPath']:
            volume.update(_replacement_transform(transformer, volume, param, context))
    for volume in docker_options.get('sharedDataVolumes', []):
        for param in ['name', 'containerPath']:
            volume.update(_replacement_transform(transformer, volume, param, context))
    new_task['id'] = str(index)
    return new_task


def _parse_parameter_sets(parameter_sets):
    """Parse parametric sweep set, and return all possible values in array.
    :param list parameter_sets: An array of parameter sets.
    """
    if not parameter_sets:
        raise ValueError('No parameter set is defined.')
    iterations = []
    for params in parameter_sets:
        try:
            start = int(params['start'])
            end = int(params['end'])
            step = int(params.get('step', 1))
        except KeyError as error:
            raise ValueError("No '{}' in parameter set".format(error.args[0]))
        except (TypeError, ValueError):
            raise ValueError("'start', 'end' and 'step' parameters must be integers.")
        if step == 0:
            raise ValueError("'step' parameter cannot be 0.")
        elif start > end and step > 0:
            raise ValueError(
                "'step' must be a negative number when 'start' is greater than 'end'")
        elif start < end and step < 0:
            raise ValueError(
                "'step' must be a positive number when 'end' is greater than 'start'")
        end = end + 1 if end >= start else end - 1
        iterations.append(range(start, end, step))
    return itertools.product(*iterations)


def _parse_repeat_task(task):
    """Parse the repeat task JSON object.
    :param dict task: The repeat task object.
    """
    if 'id' in task:
        raise ValueError("Repeat task object should not have an 'id'.")
    try:
        new_task = {'commandLine': task['commandLine']}
    except KeyError:
        raise ValueError("Repeat task must have 'commandLine'.")
    properties = {p: task.get(p) for p in _PROPS_ON_REPEAT_TASK if task.get(p)}
    new_task.update(properties)
    return new_task


def _expand_parametric_sweep(factory):
    """Parse parametric sweep task factory object, and return task list.
    :param dict factory: A loaded JSON task factory object.
    """
    try:
        permutations = _parse_parameter_sets(factory['parameterSets'])
    except (KeyError, TypeError):
        raise ValueError('Parameter set in parametric sweep task factory is missing or invalid.')
    try:
        repeat_task = _parse_repeat_task(factory['repeatTask'])
    except (KeyError, TypeError):
        raise ValueError('No repeat task is defined in parametric sweep task factory.')
    task_objs = [_transform_repeat_task(repeat_task, p, i, _transform_sweep_str)
                 for i, p in enumerate(permutations)]
    try:
        merge_task = _parse_repeat_task(factory['mergeTask'])
        merge_task['id'] = 'merge'
        merge_task['dependsOn'] = {'taskIdRanges': {'start': 0, 'end': len(task_objs) - 1}}
        task_objs.append(merge_task)
    except KeyError:  # No merge task
        pass
    return task_objs


def _expand_task_collection(factory):
    """Parse task collection task factory object, and return task list.
    :param dict factory: A loaded JSON task factory object.
    """
    try:
        tasks = factory['tasks']
    except KeyError:
        raise ValueError('No tasks are defined in task collection factory.')
    task_objs = []
    try:
        for task in tasks:
            new_task = {
                'id': task['id'],
                'commandLine': task['commandLine']}
            properties = {p: task.get(p) for p in _PROPS_ON_COLLECTION_TASK if task.get(p)}
            new_task.update(properties)
            task_objs.append(new_task)
        return task_objs
    except KeyError:
        raise ValueError("Each task in collection factory must have "
                         "'id' and 'commandLine' properties")
    except TypeError:
        raise ValueError("Task objects on collection factory invalid.")


def _expand_task_per_file(factory, fileutils):
    """Parse file iteration task factory object, and return task list.
    :param dict factory: A loaded JSON task factory object.
    """
    try:
        files = fileutils.get_container_list(factory['source'])
    except (KeyError, TypeError):
        raise ValueError('No file source is defined in file iteration task factory.')
    try:
        repeat_task = _parse_repeat_task(factory['repeatTask'])
    except (KeyError, TypeError):
        raise ValueError('No repeat task is defined in file iteration task factory.')
    task_objs = [_transform_repeat_task(repeat_task, f, i, _transform_file_str)
                 for i, f in enumerate(files)]
    try:
        merge_task = _parse_repeat_task(factory['mergeTask'])
        merge_task['id'] = 'merge'
        merge_task['dependsOn'] = {'taskIdRanges': {'start': 0, 'end': len(task_objs) - 1}}
        task_objs.append(merge_task)
    except KeyError:  # No merge task
        pass
    return task_objs


def expand_application_template(job, working_dir):
    """Expand an application template reference on a job, returning the modified job.
    :param dict job: A job specification that may contain an application template reference.
    :param string working_dir: Base folder for evaluation of relative paths (is required).
    """
    if job.get('applicationTemplateInfo') is None:
        # No application template used; nothing to do.
        return job
    _validate_job_requesting_app_template(job, working_dir)
    # Retrieve the application template and expand it with the available parameters.
    template_filepath = _resolve_template_file(job, working_dir)
    try:
        with open(template_filepath, 'r') as file_handle:
            template_str = file_handle.read()
            template_loaded = json.loads(template_str)
    except (EnvironmentError, ValueError) as error:
        raise ValueError("Failed to parse JSON loaded from '{}': {}".
                         format(template_filepath, error))
    _validate_application_template(template_loaded)
    _validate_parameter_usage(job['applicationTemplateInfo'].get('parameters'),
                              template_loaded.get('parameters'))
    job_from_template = _parse_template(template_str, template_loaded,
                                        job['applicationTemplateInfo'].get('parameters'))
    metadata = _merge_metadata(job_from_template.get('metadata'), job.get('metadata'))
    _validate_metadata(metadata)
    metadata.append({'name': 'az_batch:template_filepath', 'value': template_filepath})
    # Safety checks that "ambitious" use of
    # templating features haven't allowed someone to bypass the rules
    _validate_generated_job(job_from_template)
    # Merge the job as defined by the application template with the original job we were given
    job.update(job_from_template)
    for key in ['applicationTemplateInfo', 'templateMetadata', 'parameters']:
        job.pop(key, None)
    job['metadata'] = metadata
    return job


def expand_template(template_file, parameter_file=None):
    """Return JSON object with with the parameters replaced.
    :param str template_file: Input template file name.
    :param str parameter_file: Input parameter file name.
    """
    try:
        with open(template_file, 'r') as template:
            template_json = json.load(template)
        parameter_json = {}
        if parameter_file:
            with open(parameter_file, 'r') as parameters:
                parameter_json = json.load(parameters)
    except (EnvironmentError, ValueError) as error:
        raise ValueError("Invalid JSON file: {}".format(error))
    parameters = _get_template_params(template_json, parameter_json)
    return _parse_template(json.dumps(template_json), template_json, parameters)


def expand_task_factory(job_obj, fileutils):
    """Parse a task factory object and expand to a list of tasks.
    :param dict job_obj: The JSON job entity loaded from a template.
    :returns: a list of task entities.
    """
    task_factory = job_obj.pop('taskFactory')
    try:
        factory_type = task_factory['type']
    except KeyError:
        raise ValueError('No type property in taskFactory.')
    if factory_type == 'parametricSweep':
        return _expand_parametric_sweep(task_factory)
    elif factory_type == 'taskCollection':
        return _expand_task_collection(task_factory)
    elif factory_type == 'taskPerFile':
        return _expand_task_per_file(task_factory, fileutils)
    else:
        raise TypeError("'{}' is not a valid Task Factory type.".format(factory_type))


def construct_setup_task(existing_task, command_info, os_flavor):
    """Constructs a command line for the start task/job prep task which will
    run the setup script.
    :param dict existing_task: The original start task or job prep task.
    :param list command_info: The additional command info to add.
    :param dict os_flavor: The OS flavor of the pool.
    :returns: An updated start task or job prep task.
    """
    if existing_task:
        result = copy.deepcopy(existing_task)
    else:
        result = {}
    commands = []
    resources = []
    is_windows = None
    for cmd in command_info:
        if cmd:
            commands.append(cmd['cmdLine'])
            resources.extend(cmd.get('resourceFiles', []))
            if is_windows is None:
                is_windows = cmd['isWindows']
            elif is_windows != cmd['isWindows']:
                raise ValueError('The command is not compatible with Windows or Linux.')
    if not commands:
        return existing_task
    if result.get('commandLine'):
        commands.append(result['commandLine'])
    resources.extend(result.get('resourceFiles', []))
    if os_flavor == pool_utils.PoolOperatingSystemFlavor.WINDOWS:
        full_win_cmd = ' & '.join(commands)
        result['commandLine'] = 'cmd.exe /c "{}"'.format(full_win_cmd)
    elif os_flavor == pool_utils.PoolOperatingSystemFlavor.LINUX:
        # Escape the users command line
        full_linux_cmd = shell_escape(';'.join(commands))
        result['commandLine'] = '/bin/bash -c {}'.format(full_linux_cmd)
    else:
        raise ValueError("Unknown pool OS flavor: " + os_flavor)
    if resources:
        result['resourceFiles'] = resources
    # Must run elevated and wait for success for the setup step
    result['userIdentity'] = {'autoUser': {"elevationLevel": "admin"}}
    result['waitForSuccess'] = True
    return result


def process_job_for_output_files(job, tasks, os_flavor, file_utils):
    """Process a job and its collection of tasks for any tasks which use outputFiles.
    If a task does use outputFiles, we add to the jobs jobPrepTask for the install step.
    NOTE: This edits the task collection and job in-line!
    :param dict job: A job specification.
    :param list tasks: A list of task specifications.
    :param string os_flavor: The OS flavor of the pool.
    :returns: A dictionary with 'cmdLine' and 'resourceFiles'.
    """
    must_edit_job = False
    is_windows = True
    if job.get('jobManagerTask'):
        original_task = copy.deepcopy(job['jobManagerTask'])
        job['jobManagerTask'] = _parse_task_output_files(job['jobManagerTask'],
                                                         os_flavor,
                                                         file_utils)
        if original_task != job['jobManagerTask']:
            must_edit_job = True
    if tasks:
        for index, task in enumerate(tasks):
            tasks[index] = _parse_task_output_files(task, os_flavor, file_utils)
            if task != tasks[index]:
                must_edit_job = True
    if must_edit_job:
        resource_files = list(_FILE_EGRESS_RESOURCES)
        if os_flavor == pool_utils.PoolOperatingSystemFlavor.WINDOWS:
            setup_cmd = '(bootstrap.cmd && setup_uploader.py) > setuplog.txt 2>&1'
            resource_files.append(
                _FILE_EGRESS_PREFIX + 'bootstrap.cmd')
        elif os_flavor == pool_utils.PoolOperatingSystemFlavor.LINUX:
            setup_cmd = 'setup_uploader.py > setuplog.txt 2>&1'
            is_windows = False
        else:
            raise ValueError("Unknown pool OS flavor: " + os_flavor)
        # TODO: If we have any issues with this being hosted in GITHUB we'll have to
        # move it elsewhere (local and then upload to their storage?)
        resources = [{'blobSource': _get_output_source_url(f), 'filePath': os.path.split(f)[1]}
                     for f in resource_files]
        return {'cmdLine': setup_cmd, 'resourceFiles': resources, 'isWindows': is_windows}
    return None


def process_pool_package_references(pool):
    """Parse package reference section in the pool JSON object.
    :param dict pool: A pool specification.
    """
    if not isinstance(pool['packageReferences'], list):
        raise TypeError('PackageReferences of Pool has to be a collection.')
    os_flavor = pool_utils.get_pool_target_os_type(pool)
    return _get_installation_cmdline(pool['packageReferences'], os_flavor)


def process_task_package_references(tasks, os_flavor):
    """Parse package reference section in the task JSON object.
    :param list tasks: A collection of task specifications.
    :param str os_flavor: The OS flavor of the pool.
    """
    if not tasks:
        return
    packages = []
    included = []
    for task in tasks:
        try:
            for package in task['packageReferences']:
                if not package.get('id') or not package.get('type'):
                    raise ValueError('A PackageReference must have a type and id element.')
                if package['id'] not in included:
                    packages.append(package)
                    included.append(package['id'])
            task.pop('packageReferences')
        except KeyError:
            pass
        except TypeError:
            raise TypeError('PackageReferences of a task has to be a collection.')
    return _get_installation_cmdline(packages, os_flavor)


def post_processing(request, fileutils):
    """Parse job or task to process new resource file references.
    :param dict request: A job or task specification (or list thereof).
    """
    # Reform all new resource file references in standard ResourceFiles
    if isinstance(request, list):
        return [_process_resource_files(i, fileutils) for i in request]
    else:
        return _process_resource_files(request, fileutils)


def should_get_pool(tasks):
    """Determines if the pool (or auto pool specification) needs to be
    reviewed to determine the target operating system.
    This is required for some features which craft command lines and the
    command lines are OS dependent.
    :param list tasks: A collection of tasks to be added to the job.
    :returns: bool
    """
    # TODO: Ideally this could share code with the package reference and output files methods
    if not tasks:
        return False
    for task in tasks:
        if task.get('packageReferences'):
            return True
        if task.get('outputFiles'):
            return True
        if task.get('clientExtensions', {}).get('dockerOptions'):
            return True
    return False
