# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------

from enum import Enum

# pylint: disable=too-few-public-methods


class PoolOperatingSystemFlavor(Enum):
    WINDOWS = 'windows'
    LINUX = 'linux'


def get_pool_target_os_type(pool):
    try:
        image_publisher = pool['virtualMachineConfiguration']['imageReference']['publisher']
    except KeyError:
        image_publisher = None

    return PoolOperatingSystemFlavor.WINDOWS \
        if not image_publisher \
        or (image_publisher and image_publisher.find('MicrosoftWindowsServer') >= 0) \
        else PoolOperatingSystemFlavor.LINUX
