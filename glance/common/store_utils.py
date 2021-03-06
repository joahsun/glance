#    Copyright 2014 IBM Corp.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import sys

from oslo.config import cfg

from glance.common import exception
from glance.common import utils
import glance.db as db_api
from glance.openstack.common import gettextutils
import glance.openstack.common.log as logging
from glance import scrubber
import glance.store as store_api

_LE = gettextutils._LE
_LW = gettextutils._LW

LOG = logging.getLogger(__name__)

store_utils_opts = [
    cfg.BoolOpt('use_user_token', default=True,
                help=_('Whether to pass through the user token when '
                       'making requests to the registry.')),
]

CONF = cfg.CONF
CONF.register_opts(store_utils_opts)


def safe_delete_from_backend(context, image_id, location):
    """
    Given a location, delete an image from the store and
    update location status to db.

    This function try to handle all known exceptions which might be raised
    by those calls on store and DB modules in its implementation.

    :param context: The request context
    :param image_id: The image identifier
    :param location: The image location entry
    """

    try:
        ret = store_api.delete_from_backend(context, location['url'])
        location['status'] = 'deleted'
        if 'id' in location:
            db_api.get_api().image_location_delete(context, image_id,
                                                   location['id'], 'deleted')
        return ret
    except exception.NotFound:
        msg = _LW('Failed to delete image %s in store from URI') % image_id
        LOG.warn(msg)
    except exception.StoreDeleteNotSupported as e:
        LOG.warn(utils.exception_to_str(e))
    except store_api.UnsupportedBackend:
        exc_type = sys.exc_info()[0].__name__
        msg = (_LE('Failed to delete image %(image_id)s from store: %(exc)s') %
               dict(image_id=image_id, exc=exc_type))
        LOG.error(msg)


def schedule_delayed_delete_from_backend(context, image_id, location):
    """
    Given a location, schedule the deletion of an image location and
    update location status to db.

    :param context: The request context
    :param image_id: The image identifier
    :param location: The image location entry
    """

    (file_queue, _db_queue) = scrubber.get_scrub_queues()
    if not CONF.use_user_token:
        context = None
    # TODO(zhiyan): using location status to do image scrub.
    ret = file_queue.add_location(image_id, location, user_context=context)
    if ret:
        location['status'] = 'pending_delete'
        if 'id' in location:
            # NOTE(zhiyan): New added image location entry will has no 'id'
            # field since it has not been saved to DB.
            db_api.get_api().image_location_delete(context, image_id,
                                                   location['id'],
                                                   'pending_delete')
        else:
            db_api.get_api().image_location_add(context, image_id, location)

    return ret


def delete_image_location_from_backend(context, image_id, location):
    """
    Given a location, immediately or schedule the deletion of an image
    location and update location status to db.

    :param context: The request context
    :param image_id: The image identifier
    :param location: The image location entry
    """

    deleted = False
    if CONF.delayed_delete:
        deleted = schedule_delayed_delete_from_backend(context,
                                                       image_id, location)
    if not deleted:
        # NOTE(zhiyan) If image metadata has not been saved to DB
        # such as uploading process failure then we can't use
        # location status mechanism to support image pending delete.
        safe_delete_from_backend(context, image_id, location)
