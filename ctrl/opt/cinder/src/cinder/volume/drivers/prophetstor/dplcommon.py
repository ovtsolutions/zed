# Copyright (c) 2014 ProphetStor, Inc.
# All Rights Reserved.
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
"""
Implementation of the class of ProphetStor DPL storage adapter of Federator.
    # v2.0.1 Consistency group support
    # v2.0.2 Pool aware scheduler
    # v2.0.3 Consistency group modification support
    # v2.0.4 Port ProphetStor driver to use new driver model
    # v2.0.5 Move from httplib to requests
"""

import base64
import errno
import json
import random
import time

from oslo_log import log as logging
from oslo_service import loopingcall
from oslo_utils import units
import requests
import six
from six.moves import http_client

from cinder.common import constants
from cinder import exception
from cinder.i18n import _
from cinder import objects
from cinder.objects import fields
from cinder.volume import driver
from cinder.volume.drivers.prophetstor import options
from cinder.volume.drivers.san import san
from cinder.volume import volume_utils

LOG = logging.getLogger(__name__)

CONNECTION_RETRY = 10
MAXSNAPSHOTS = 1024
DISCOVER_SERVER_TYPE = 'dpl'
DPL_BLOCKSTOR = '/dpl_blockstor'
DPL_SYSTEM = '/dpl_system'

DPL_VER_V1 = 'v1'
DPL_OBJ_POOL = 'dpl_pool'
DPL_OBJ_DISK = 'dpl_disk'
DPL_OBJ_VOLUME = 'dpl_volume'
DPL_OBJ_VOLUMEGROUP = 'dpl_volgroup'
DPL_OBJ_SNAPSHOT = 'cdmi_snapshots'
DPL_OBJ_EXPORT = 'dpl_export'

DPL_OBJ_REPLICATION = 'cdmi_replication'
DPL_OBJ_TARGET = 'dpl_target'
DPL_OBJ_SYSTEM = 'dpl_system'
DPL_OBJ_SNS = 'sns_table'


class DPLCommand(object):
    """DPL command interface."""

    def __init__(self, ip, port, username, password, cert_verify=False,
                 cert_path=None):
        self.ip = ip
        self.port = port
        self.username = username
        self.password = password
        self.cert_verify = cert_verify
        self.cert_path = cert_path

    def send_cmd(self, method, url, params, expected_status):
        """Send command to DPL."""
        retcode = 0
        data = {}
        header = {'Content-Type': 'application/cdmi-container',
                  'Accept': 'application/cdmi-container',
                  'x-cdmi-specification-version': '1.0.2'}
        # base64 encode the username and password
        auth = base64.encodebytes('%s:%s'
                                  % (self.username,
                                      self.password)).replace('\n', '')
        header['Authorization'] = 'Basic %s' % auth

        if not params:
            payload = None
        else:
            try:
                payload = json.dumps(params, ensure_ascii=False)
                payload.encode('utf-8')
            except Exception as e:
                LOG.error('JSON encode params %(param)s error:'
                          ' %(status)s.', {'param': params, 'status': e})
                retcode = errno.EINVAL

        retry = CONNECTION_RETRY
        func = getattr(requests, method.lower())

        cert_path = False
        if self.cert_verify:
            cert_path = self.cert_path
        else:
            cert_path = False

        while (retry):
            try:
                r = func(
                    url="https://%s:%s%s" % (self.ip, self.port, url),
                    data=payload, headers=header, verify=cert_path)

                if r.status_code == http_client.SERVICE_UNAVAILABLE:
                    LOG.error("The flexvisor service is unavailable.")
                    continue
                else:
                    break
            except Exception as e:
                msg = (_("failed to %(method)s due to %(error)s")
                       % {"method": method, "error": six.text_type(e)})
                LOG.error(msg)
                raise exception.VolumeBackendAPIException(data=msg)

        if (r.status_code in expected_status and
                r.status_code == http_client.NOT_FOUND):
            retcode = errno.ENODATA
        elif r.status_code not in expected_status:
            LOG.error('%(method)s %(url)s unexpected response status: '
                      '%(response)s (expects: %(expects)s).',
                      {'method': method,
                       'url': url,
                       'response': http_client.responses[r.status_code],
                       'expects': expected_status})
            if r.status_code == http_client.UNAUTHORIZED:
                raise exception.NotAuthorized
            else:
                retcode = errno.EIO
        elif r.status_code is http_client.NOT_FOUND:
            retcode = errno.ENODATA
        elif r.status_code is http_client.ACCEPTED:
            retcode = errno.EAGAIN
            try:
                data = r.json()
            except (TypeError, ValueError) as e:
                LOG.error('Call to json.loads() raised an exception: %s.',
                          e)
                retcode = errno.ENOEXEC
            except Exception as e:
                LOG.error('Read response raised an exception: %s.',
                          e)
                retcode = errno.ENOEXEC
        elif (r.status_code in [http_client.OK, http_client.CREATED] and
                http_client.NO_CONTENT not in expected_status):
            try:
                data = r.json()
            except (TypeError, ValueError) as e:
                LOG.error('Call to json.loads() raised an exception: %s.',
                          e)
                retcode = errno.ENOEXEC
            except Exception as e:
                LOG.error('Read response raised an exception: %s.',
                          e)
                retcode = errno.ENOEXEC

        return retcode, data


class DPLVolume(object):

    def __init__(self, dplServer, dplPort, dplUser, dplPassword,
                 cert_verify=False, cert_path=None):
        self.objCmd = DPLCommand(
            dplServer, dplPort, dplUser, dplPassword, cert_verify=cert_verify,
            cert_path=cert_path)

    def _execute(self, method, url, params, expected_status):
        if self.objCmd:
            return self.objCmd.send_cmd(method, url, params, expected_status)
        else:
            return -1, None

    def _gen_snapshot_url(self, vdevid, snapshotid):
        snapshot_url = '/%s/%s/%s' % (vdevid, DPL_OBJ_SNAPSHOT, snapshotid)
        return snapshot_url

    def get_server_info(self):
        method = 'GET'
        url = ('/%s/%s/' % (DPL_VER_V1, DPL_OBJ_SYSTEM))
        return self._execute(method, url, None,
                             [http_client.OK, http_client.ACCEPTED])

    def create_vdev(self, volumeID, volumeName, volumeDesc, poolID, volumeSize,
                    fthinprovision=True, maximum_snapshot=MAXSNAPSHOTS,
                    snapshot_quota=None):
        method = 'PUT'
        metadata = {}
        params = {}
        url = '/%s/%s/%s/' % (DPL_VER_V1, DPL_OBJ_VOLUME, volumeID)

        if volumeName is None or volumeName == '':
            metadata['display_name'] = volumeID
        else:
            metadata['display_name'] = volumeName
        metadata['display_description'] = volumeDesc
        metadata['pool_uuid'] = poolID
        metadata['total_capacity'] = volumeSize
        metadata['maximum_snapshot'] = maximum_snapshot
        if snapshot_quota is not None:
            metadata['snapshot_quota'] = int(snapshot_quota)
        metadata['properties'] = dict(thin_provision=fthinprovision)
        params['metadata'] = metadata
        return self._execute(method,
                             url, params,
                             [http_client.OK, http_client.ACCEPTED,
                              http_client.CREATED])

    def extend_vdev(self, volumeID, volumeName, volumeDesc, volumeSize,
                    maximum_snapshot=MAXSNAPSHOTS, snapshot_quota=None):
        method = 'PUT'
        metadata = {}
        params = {}
        url = '/%s/%s/%s/' % (DPL_VER_V1, DPL_OBJ_VOLUME, volumeID)

        if volumeName is None or volumeName == '':
            metadata['display_name'] = volumeID
        else:
            metadata['display_name'] = volumeName
        metadata['display_description'] = volumeDesc
        metadata['total_capacity'] = int(volumeSize)
        metadata['maximum_snapshot'] = maximum_snapshot
        if snapshot_quota is not None:
            metadata['snapshot_quota'] = snapshot_quota
        params['metadata'] = metadata
        return self._execute(method,
                             url, params,
                             [http_client.OK, http_client.ACCEPTED,
                              http_client.CREATED])

    def delete_vdev(self, volumeID, force=True):
        method = 'DELETE'
        metadata = {}
        params = {}
        url = '/%s/%s/%s/' % (DPL_VER_V1, DPL_OBJ_VOLUME, volumeID)

        metadata['force'] = force
        params['metadata'] = metadata
        return self._execute(method,
                             url, params,
                             [http_client.OK, http_client.ACCEPTED,
                              http_client.NOT_FOUND, http_client.NO_CONTENT])

    def create_vdev_from_snapshot(self, vdevID, vdevDisplayName, vdevDesc,
                                  snapshotID, poolID, fthinprovision=True,
                                  maximum_snapshot=MAXSNAPSHOTS,
                                  snapshot_quota=None):
        method = 'PUT'
        metadata = {}
        params = {}
        url = '/%s/%s/%s/' % (DPL_VER_V1, DPL_OBJ_VOLUME, vdevID)
        metadata['snapshot_operation'] = 'copy'
        if vdevDisplayName is None or vdevDisplayName == "":
            metadata['display_name'] = vdevID
        else:
            metadata['display_name'] = vdevDisplayName
        metadata['display_description'] = vdevDesc
        metadata['pool_uuid'] = poolID
        metadata['properties'] = {}
        metadata['maximum_snapshot'] = maximum_snapshot
        if snapshot_quota:
            metadata['snapshot_quota'] = snapshot_quota
        metadata['properties'] = dict(thin_provision=fthinprovision)

        params['metadata'] = metadata
        params['copy'] = self._gen_snapshot_url(vdevID, snapshotID)
        return self._execute(method,
                             url, params,
                             [http_client.OK, http_client.ACCEPTED,
                              http_client.CREATED])

    def spawn_vdev_from_snapshot(self, new_vol_id, src_vol_id,
                                 vol_display_name, description, snap_id):
        method = 'PUT'
        params = {}
        metadata = {}
        url = '/%s/%s/%s/' % (DPL_VER_V1, DPL_OBJ_VOLUME, new_vol_id)

        metadata['snapshot_operation'] = 'spawn'
        if vol_display_name is None or vol_display_name == '':
            metadata['display_name'] = new_vol_id
        else:
            metadata['display_name'] = vol_display_name
        metadata['display_description'] = description
        params['metadata'] = metadata
        params['copy'] = self._gen_snapshot_url(src_vol_id, snap_id)

        return self._execute(method, url, params,
                             [http_client.OK, http_client.ACCEPTED,
                              http_client.CREATED])

    def get_pools(self):
        method = 'GET'
        url = '/%s/%s/' % (DPL_VER_V1, DPL_OBJ_POOL)
        return self._execute(method, url, None, [http_client.OK])

    def get_pool(self, poolid):
        method = 'GET'
        url = '/%s/%s/%s/' % (DPL_VER_V1, DPL_OBJ_POOL, poolid)
        return self._execute(method, url, None,
                             [http_client.OK, http_client.ACCEPTED])

    def clone_vdev(self, SourceVolumeID, NewVolumeID, poolID, volumeName,
                   volumeDesc, volumeSize, fthinprovision=True,
                   maximum_snapshot=MAXSNAPSHOTS, snapshot_quota=None):
        method = 'PUT'
        params = {}
        metadata = {}
        url = '/%s/%s/%s/' % (DPL_VER_V1, DPL_OBJ_VOLUME, NewVolumeID)
        metadata["snapshot_operation"] = "clone"
        if volumeName is None or volumeName == '':
            metadata["display_name"] = NewVolumeID
        else:
            metadata["display_name"] = volumeName
        metadata["display_description"] = volumeDesc
        metadata["pool_uuid"] = poolID
        metadata["total_capacity"] = volumeSize
        metadata["maximum_snapshot"] = maximum_snapshot
        if snapshot_quota:
            metadata["snapshot_quota"] = snapshot_quota
        metadata["properties"] = dict(thin_provision=fthinprovision)
        params["metadata"] = metadata
        params["copy"] = SourceVolumeID

        return self._execute(method,
                             url, params,
                             [http_client.OK, http_client.CREATED,
                              http_client.ACCEPTED])

    def create_vdev_snapshot(self, vdevid, snapshotid, snapshotname='',
                             snapshotdes='', isgroup=False):
        method = 'PUT'
        metadata = {}
        params = {}
        if isgroup:
            url = '/%s/%s/%s/' % (DPL_VER_V1, DPL_OBJ_VOLUMEGROUP, vdevid)
        else:
            url = '/%s/%s/%s/' % (DPL_VER_V1, DPL_OBJ_VOLUME, vdevid)

        if not snapshotname:
            metadata['display_name'] = snapshotid
        else:
            metadata['display_name'] = snapshotname
        metadata['display_description'] = snapshotdes

        params['metadata'] = metadata
        params['snapshot'] = snapshotid

        return self._execute(method,
                             url, params,
                             [http_client.OK, http_client.CREATED,
                              http_client.ACCEPTED])

    def get_vdev(self, vdevid):
        method = 'GET'
        url = '/%s/%s/%s/' % (DPL_VER_V1, DPL_OBJ_VOLUME, vdevid)

        return self._execute(method,
                             url, None,
                             [http_client.OK, http_client.ACCEPTED,
                              http_client.NOT_FOUND])

    def get_vdev_status(self, vdevid, eventid):
        method = 'GET'
        url = ('/%s/%s/%s/?event_uuid=%s' % (DPL_VER_V1, DPL_OBJ_VOLUME,
                                             vdevid, eventid))

        return self._execute(method,
                             url, None,
                             [http_client.OK, http_client.NOT_FOUND])

    def get_pool_status(self, poolid, eventid):
        method = 'GET'
        url = ('/%s/%s/%s/?event_uuid=%s' % (DPL_VER_V1, DPL_OBJ_POOL,
                                             poolid, eventid))

        return self._execute(method,
                             url, None,
                             [http_client.OK, http_client.NOT_FOUND])

    def assign_vdev(self, vdevid, iqn, lunname, portal, lunid=0):
        method = 'PUT'
        metadata = {}
        exports = {}
        params = {}
        url = '/%s/%s/%s/' % (DPL_VER_V1, DPL_OBJ_VOLUME, vdevid)

        metadata['export_operation'] = 'assign'
        exports['Network/iSCSI'] = {}
        target_info = {}
        target_info['logical_unit_number'] = 0
        target_info['logical_unit_name'] = lunname
        permissions = []
        portals = []
        portals.append(portal)
        permissions.append(iqn)
        target_info['permissions'] = permissions
        target_info['portals'] = portals
        exports['Network/iSCSI'] = target_info

        params['metadata'] = metadata
        params['exports'] = exports

        return self._execute(method,
                             url, params,
                             [http_client.OK, http_client.ACCEPTED,
                              http_client.CREATED])

    def assign_vdev_fc(self, vdevid, targetwwpn, initiatorwwpn, lunname,
                       lunid=-1):
        method = 'PUT'
        metadata = {}
        exports = {}
        params = {}
        url = '/%s/%s/%s/' % (DPL_VER_V1, DPL_OBJ_VOLUME, vdevid)
        metadata['export_operation'] = 'assign'
        exports['Network/FC'] = {}
        target_info = {}
        target_info['target_identifier'] = targetwwpn
        target_info['logical_unit_number'] = lunid
        target_info['logical_unit_name'] = lunname
        target_info['permissions'] = initiatorwwpn
        exports['Network/FC'] = target_info

        params['metadata'] = metadata
        params['exports'] = exports

        return self._execute(method,
                             url, params,
                             [http_client.OK, http_client.ACCEPTED,
                              http_client.CREATED])

    def unassign_vdev(self, vdevid, initiatorIqn, targetIqn=''):
        method = 'PUT'
        metadata = {}
        exports = {}
        params = {}
        url = '/%s/%s/%s/' % (DPL_VER_V1, DPL_OBJ_VOLUME, vdevid)

        metadata['export_operation'] = 'unassign'
        params['metadata'] = metadata

        exports['Network/iSCSI'] = {}
        exports['Network/iSCSI']['target_identifier'] = targetIqn
        permissions = []
        permissions.append(initiatorIqn)
        exports['Network/iSCSI']['permissions'] = permissions

        params['exports'] = exports

        return self._execute(method,
                             url, params,
                             [http_client.OK, http_client.ACCEPTED,
                              http_client.NO_CONTENT, http_client.NOT_FOUND])

    def unassign_vdev_fc(self, vdevid, targetwwpn, initiatorwwpns):
        method = 'PUT'
        metadata = {}
        exports = {}
        params = {}
        url = '/%s/%s/%s/' % (DPL_VER_V1, DPL_OBJ_VOLUME, vdevid)

        metadata['export_operation'] = 'unassign'
        params['metadata'] = metadata

        exports['Network/FC'] = {}
        exports['Network/FC']['target_identifier'] = targetwwpn
        permissions = initiatorwwpns
        exports['Network/FC']['permissions'] = permissions

        params['exports'] = exports

        return self._execute(method,
                             url, params,
                             [http_client.OK, http_client.ACCEPTED,
                              http_client.NO_CONTENT, http_client.NOT_FOUND])

    def delete_vdev_snapshot(self, objID, snapshotID, isGroup=False):
        method = 'DELETE'
        if isGroup:
            url = ('/%s/%s/%s/%s/%s/' % (DPL_VER_V1,
                                         DPL_OBJ_VOLUMEGROUP,
                                         objID,
                                         DPL_OBJ_SNAPSHOT, snapshotID))
        else:
            url = ('/%s/%s/%s/%s/%s/' % (DPL_VER_V1,
                                         DPL_OBJ_VOLUME, objID,
                                         DPL_OBJ_SNAPSHOT, snapshotID))

        return self._execute(method,
                             url, None,
                             [http_client.OK, http_client.ACCEPTED,
                              http_client.NO_CONTENT, http_client.NOT_FOUND])

    def rollback_vdev(self, vdevid, snapshotid):
        method = 'PUT'
        params = {}
        url = '/%s/%s/%s/' % (DPL_VER_V1, DPL_OBJ_VOLUME, vdevid)

        params['copy'] = self._gen_snapshot_url(vdevid, snapshotid)

        return self._execute(method,
                             url, params,
                             [http_client.OK, http_client.ACCEPTED])

    def list_vdev_snapshots(self, vdevid, isGroup=False):
        method = 'GET'
        if isGroup:
            url = ('/%s/%s/%s/%s/' % (DPL_VER_V1, DPL_OBJ_VOLUMEGROUP, vdevid,
                                      DPL_OBJ_SNAPSHOT))
        else:
            url = ('/%s/%s/%s/%s/' % (DPL_VER_V1, DPL_OBJ_VOLUME,
                                      vdevid, DPL_OBJ_SNAPSHOT))

        return self._execute(method,
                             url, None,
                             [http_client.OK])

    def query_vdev_snapshot(self, vdevid, snapshotID, isGroup=False):
        method = 'GET'
        if isGroup:
            url = ('/%s/%s/%s/%s/%s/' % (DPL_VER_V1, DPL_OBJ_VOLUMEGROUP,
                                         vdevid, DPL_OBJ_SNAPSHOT, snapshotID))
        else:
            url = ('/%s/%s/%s/%s/%s/' % (DPL_VER_V1, DPL_OBJ_VOLUME, vdevid,
                                         DPL_OBJ_SNAPSHOT, snapshotID))

        return self._execute(method,
                             url, None,
                             [http_client.OK])

    def create_target(self, targetID, protocol, displayName, targetAddress,
                      description=''):
        method = 'PUT'
        params = {}
        url = '/%s/%s/%s/' % (DPL_VER_V1, DPL_OBJ_EXPORT, targetID)
        params['metadata'] = {}
        metadata = params['metadata']
        metadata['type'] = 'target'
        metadata['protocol'] = protocol
        if displayName is None or displayName == '':
            metadata['display_name'] = targetID
        else:
            metadata['display_name'] = displayName
        metadata['display_description'] = description
        metadata['address'] = targetAddress
        return self._execute(method, url, params, [http_client.OK])

    def get_target(self, targetID):
        method = 'GET'
        url = '/%s/%s/%s/' % (DPL_VER_V1, DPL_OBJ_EXPORT, targetID)
        return self._execute(method, url, None, [http_client.OK])

    def delete_target(self, targetID):
        method = 'DELETE'
        url = '/%s/%s/%s/' % (DPL_VER_V1, DPL_OBJ_EXPORT, targetID)
        return self._execute(method,
                             url, None,
                             [http_client.OK, http_client.ACCEPTED,
                              http_client.NOT_FOUND])

    def get_target_list(self, type='target'):
        # type = target/initiator
        method = 'GET'
        if type is None:
            url = '/%s/%s/' % (DPL_VER_V1, DPL_OBJ_EXPORT)
        else:
            url = '/%s/%s/?type=%s' % (DPL_VER_V1, DPL_OBJ_EXPORT, type)
        return self._execute(method, url, None, [http_client.OK])

    def get_sns_table(self, wwpn):
        method = 'PUT'
        params = {}
        url = '/%s/%s/%s/' % (DPL_VER_V1, DPL_OBJ_EXPORT, DPL_OBJ_SNS)
        params['metadata'] = {}
        params['metadata']['protocol'] = 'fc'
        params['metadata']['address'] = str(wwpn)
        return self._execute(method, url, params, [http_client.OK])

    def create_vg(self, groupID, groupName, groupDesc='', listVolume=None,
                  maxSnapshots=MAXSNAPSHOTS, rotationSnapshot=True):
        method = 'PUT'
        metadata = {}
        params = {}
        properties = {}
        url = '/%s/%s/' % (DPL_OBJ_VOLUMEGROUP, groupID)
        if listVolume:
            metadata['volume'] = listVolume
        else:
            metadata['volume'] = []
        metadata['display_name'] = groupName
        metadata['display_description'] = groupDesc
        metadata['maximum_snapshot'] = maxSnapshots
        properties['snapshot_rotation'] = rotationSnapshot
        metadata['properties'] = properties
        params['metadata'] = metadata
        return self._execute(method, url, params,
                             [http_client.OK, http_client.ACCEPTED,
                              http_client.CREATED])

    def get_vg(self, groupID):
        method = 'GET'
        url = '/%s/%s/' % (DPL_OBJ_VOLUMEGROUP, groupID)
        return self._execute(method, url, None, [http_client.OK])

    def delete_vg(self, groupID, force=True):
        method = 'DELETE'
        metadata = {}
        params = {}
        url = '/%s/%s/' % (DPL_OBJ_VOLUMEGROUP, groupID)
        metadata['force'] = force
        params['metadata'] = metadata
        return self._execute(method, url, params,
                             [http_client.NO_CONTENT, http_client.NOT_FOUND])

    def join_vg(self, volumeID, groupID):
        method = 'PUT'
        metadata = {}
        params = {}
        url = '/%s/%s/' % (DPL_OBJ_VOLUMEGROUP, groupID)
        metadata['volume_group_operation'] = 'join'
        metadata['volume'] = []
        metadata['volume'].append(volumeID)
        params['metadata'] = metadata
        return self._execute(method, url, params,
                             [http_client.OK, http_client.ACCEPTED])

    def leave_vg(self, volumeID, groupID):
        method = 'PUT'
        metadata = {}
        params = {}
        url = '/%s/%s/' % (DPL_OBJ_VOLUMEGROUP, groupID)
        metadata['volume_group_operation'] = 'leave'
        metadata['volume'] = []
        metadata['volume'].append(volumeID)
        params['metadata'] = metadata
        return self._execute(method, url, params,
                             [http_client.OK, http_client.ACCEPTED])


class DPLCOMMONDriver(driver.CloneableImageVD,
                      driver.BaseVD):
    """Class of dpl storage adapter."""
    VERSION = '2.0.5'

    # ThirdPartySystems wiki page
    CI_WIKI_NAME = "ProphetStor_CI"

    # TODO(jsbryant) Remove driver in the 'U' release if CI is not fixed.
    SUPPORTED = False

    def __init__(self, *args, **kwargs):
        cert_path = None
        cert_verify = False
        super(DPLCOMMONDriver, self).__init__(*args, **kwargs)
        if self.configuration:
            self.configuration.append_config_values(options.DPL_OPTS)
            self.configuration.append_config_values(san.san_opts)
            cert_verify = self.configuration.driver_ssl_cert_verify
            cert_path = self.configuration.driver_ssl_cert_path

        if cert_verify:
            if not cert_path:
                LOG.warning(
                    "Flexvisor: cert_verify is enabled but required cert_path"
                    " option is missing.")
                cert_path = None
        else:
            cert_path = None

        self.dpl = DPLVolume(self.configuration.san_ip,
                             self.configuration.dpl_port,
                             self.configuration.san_login,
                             self.configuration.san_password,
                             cert_verify=cert_verify,
                             cert_path=cert_path)
        self._stats = {}

    @staticmethod
    def get_driver_options():
        return options.DPL_OPTS

    def _convert_size_GB(self, size):
        s = round(float(size) / units.Gi, 2)
        if s > 0:
            return s
        else:
            return 0

    def _conver_uuid2hex(self, strID):
        if strID:
            return strID.replace('-', '')
        else:
            return None

    def _get_event_uuid(self, output):
        ret = 0
        event_uuid = ""

        if (type(output) is dict and
                output.get("metadata") and output["metadata"]):
            if (output["metadata"].get("event_uuid") and
                    output["metadata"]["event_uuid"]):
                event_uuid = output["metadata"]["event_uuid"]
            else:
                ret = errno.EINVAL
        else:
            ret = errno.EINVAL
        return ret, event_uuid

    def _wait_event(self, callFun, objuuid, eventid=None):
        nRetry = 30
        fExit = False
        status = {}
        status['state'] = 'error'
        status['output'] = {}
        while nRetry:
            try:
                if eventid:
                    ret, output = callFun(
                        self._conver_uuid2hex(objuuid),
                        self._conver_uuid2hex(eventid))
                else:
                    ret, output = callFun(self._conver_uuid2hex(objuuid))

                if ret == 0:
                    if output['completionStatus'] == 'Complete':
                        fExit = True
                        status['state'] = 'available'
                        status['output'] = output
                    elif output['completionStatus'] == 'Error':
                        fExit = True
                        status['state'] = 'error'
                        raise loopingcall.LoopingCallDone(retvalue=False)
                    else:
                        nsleep = random.randint(0, 10)
                        value = round(float(nsleep) / 10, 2)
                        time.sleep(value)
                elif ret == errno.ENODATA:
                    status['state'] = 'deleted'
                    fExit = True
                else:
                    nRetry -= 1
                    time.sleep(3)
                    continue

            except Exception as e:
                LOG.error('Flexvisor failed to get event %(volume)s '
                          '(%(status)s).',
                          {'volume': eventid, 'status': e})
                raise loopingcall.LoopingCallDone(retvalue=False)

            if fExit is True:
                break
        return status

    def _join_volume_group(self, volume, cgId):
        # Join volume group if consistency group id not empty
        msg = ''
        try:
            ret, output = self.dpl.join_vg(
                self._conver_uuid2hex(volume['id']),
                self._conver_uuid2hex(cgId))
        except Exception as e:
            ret = errno.EFAULT
            msg = _('Fexvisor failed to add volume %(id)s '
                    'due to %(reason)s.') % {"id": volume['id'],
                                             "reason": six.text_type(e)}
        if ret:
            if not msg:
                msg = _('Flexvisor failed to add volume %(id)s '
                        'to group %(cgid)s.') % {'id': volume['id'],
                                                 'cgid': cgId}
            raise exception.VolumeBackendAPIException(data=msg)
        else:
            LOG.info('Flexvisor succeeded to add volume %(id)s to '
                     'group %(cgid)s.',
                     {'id': volume['id'], 'cgid': cgId})

    def _leave_volume_group(self, volume, cgId):
        # Leave volume group if consistency group id not empty
        msg = ''
        try:
            ret, output = self.dpl.leave_vg(
                self._conver_uuid2hex(volume['id']),
                self._conver_uuid2hex(cgId))
        except Exception as e:
            ret = errno.EFAULT
            msg = _('Fexvisor failed to remove volume %(id)s '
                    'due to %(reason)s.') % {"id": volume['id'],
                                             "reason": six.text_type(e)}
        if ret:
            if not msg:
                msg = _('Flexvisor failed to remove volume %(id)s '
                        'from group %(cgid)s.') % {'id': volume['id'],
                                                   'cgid': cgId}
            raise exception.VolumeBackendAPIException(data=msg)
        else:
            LOG.info('Flexvisor succeeded to remove volume %(id)s from '
                     'group %(cgid)s.',
                     {'id': volume['id'], 'cgid': cgId})

    def _get_snapshotid_of_vgsnapshot(self, vgID, vgsnapshotID, volumeID):
        snapshotID = None
        ret, out = self.dpl.query_vdev_snapshot(vgID, vgsnapshotID, True)
        if ret == 0:
            volumes = out.get('metadata', {}).get('member', {})
            if volumes:
                snapshotID = volumes.get(volumeID, None)
        else:
            msg = _('Flexvisor failed to get snapshot id of volume '
                    '%(id)s from group %(vgid)s.') % {'id': volumeID,
                                                      'vgid': vgID}
            raise exception.VolumeBackendAPIException(data=msg)
        if not snapshotID:
            msg = _('Flexvisor could not find volume %(id)s snapshot in'
                    ' the group %(vgid)s snapshot '
                    '%(vgsid)s.') % {'id': volumeID, 'vgid': vgID,
                                     'vgsid': vgsnapshotID}
            raise exception.VolumeBackendAPIException(data=msg)
        return snapshotID

    def create_export(self, context, volume, connector):
        pass

    def ensure_export(self, context, volume):
        pass

    def remove_export(self, context, volume):
        pass

    def _create_consistencygroup(self, context, group):
        """Creates a consistencygroup."""
        LOG.info('Start to create consistency group: %(group_name)s '
                 'id: %(id)s',
                 {'group_name': group.name, 'id': group.id})
        model_update = {'status': fields.GroupStatus.AVAILABLE}
        try:
            ret, output = self.dpl.create_vg(
                self._conver_uuid2hex(group.id),
                group.name,
                group.description)
            if ret:
                msg = _('Failed to create consistency group '
                        '%(id)s:%(ret)s.') % {'id': group.id,
                                              'ret': ret}
                raise exception.VolumeBackendAPIException(data=msg)
            else:
                return model_update
        except Exception as e:
            msg = _('Failed to create consistency group '
                    '%(id)s due to %(reason)s.') % {'id': group.id,
                                                    'reason': six.text_type(e)}
            raise exception.VolumeBackendAPIException(data=msg)

    def _delete_consistencygroup(self, context, group, volumes):
        """Delete a consistency group."""
        ret = 0
        volumes = self.db.volume_get_all_by_group(
            context, group.id)
        model_update = {}
        model_update['status'] = group.status
        LOG.info('Start to delete consistency group: %(cg_name)s',
                 {'cg_name': group.id})
        try:
            self.dpl.delete_vg(self._conver_uuid2hex(group.id))
        except Exception as e:
            msg = _('Failed to delete consistency group %(id)s '
                    'due to %(reason)s.') % {'id': group.id,
                                             'reason': six.text_type(e)}
            raise exception.VolumeBackendAPIException(data=msg)

        for volume_ref in volumes:
            try:
                self.dpl.delete_vdev(self._conver_uuid2hex(volume_ref['id']))
                volume_ref['status'] = 'deleted'
            except Exception:
                ret = errno.EFAULT
                volume_ref['status'] = 'error_deleting'
                model_update['status'] = (
                    fields.GroupStatus.ERROR_DELETING)
        if ret == 0:
            model_update['status'] = fields.GroupStatus.DELETED
        return model_update, volumes

    def _create_cgsnapshot(self, context, cgsnapshot, snapshots):
        """Creates a cgsnapshot."""
        snapshots = objects.SnapshotList().get_all_for_group_snapshot(
            context, cgsnapshot.id)
        model_update = {}
        LOG.info('Start to create cgsnapshot for consistency group'
                 ': %(group_name)s',
                 {'group_name': cgsnapshot.group_id})
        try:
            self.dpl.create_vdev_snapshot(
                self._conver_uuid2hex(cgsnapshot.group_id),
                self._conver_uuid2hex(cgsnapshot.id),
                cgsnapshot.name,
                '',
                True)
            for snapshot in snapshots:
                snapshot.status = fields.SnapshotStatus.AVAILABLE
        except Exception as e:
            msg = _('Failed to create cg snapshot %(id)s '
                    'due to %(reason)s.') % {'id': cgsnapshot.id,
                                             'reason': six.text_type(e)}
            raise exception.VolumeBackendAPIException(data=msg)

        model_update['status'] = 'available'

        return model_update, snapshots

    def _delete_cgsnapshot(self, context, cgsnapshot, snapshots):
        """Deletes a cgsnapshot."""
        snapshots = objects.SnapshotList().get_all_for_group_snapshot(
            context, cgsnapshot.id)
        model_update = {}
        model_update['status'] = cgsnapshot.status
        LOG.info('Delete cgsnapshot %(snap_name)s for consistency group: '
                 '%(group_name)s',
                 {'snap_name': cgsnapshot.id,
                  'group_name': cgsnapshot.group_id})
        try:
            self.dpl.delete_vdev_snapshot(
                self._conver_uuid2hex(cgsnapshot.group_id),
                self._conver_uuid2hex(cgsnapshot.id), True)
            for snapshot in snapshots:
                snapshot.status = fields.SnapshotStatus.DELETED
        except Exception as e:
            msg = _('Failed to delete cgsnapshot %(id)s due to '
                    '%(reason)s.') % {'id': cgsnapshot.id,
                                      'reason': six.text_type(e)}
            raise exception.VolumeBackendAPIException(data=msg)

        model_update['status'] = 'deleted'
        return model_update, snapshots

    def update_group(self, context, group, add_volumes=None,
                     remove_volumes=None):
        addvollist = []
        removevollist = []
        cgid = group.id
        vid = ''
        model_update = {'status': fields.GroupStatus.AVAILABLE}
        if not volume_utils.is_group_a_cg_snapshot_type(group):
            raise NotImplementedError()
        # Get current group info in backend storage.
        ret, output = self.dpl.get_vg(self._conver_uuid2hex(cgid))
        if ret == 0:
            group_members = output.get('children', [])

        if add_volumes:
            addvollist = add_volumes
        if remove_volumes:
            removevollist = remove_volumes

        # Process join volumes.
        try:
            for volume in addvollist:
                vid = volume['id']
                # Verify the volume exists in the group or not.
                if self._conver_uuid2hex(vid) in group_members:
                    continue
                self._join_volume_group(volume, cgid)
        except Exception as e:
            msg = _("Fexvisor failed to join the volume %(vol)s in the "
                    "group %(group)s due to "
                    "%(ret)s.") % {"vol": vid, "group": cgid,
                                   "ret": six.text_type(e)}
            raise exception.VolumeBackendAPIException(data=msg)
        # Process leave volumes.
        try:
            for volume in removevollist:
                vid = volume['id']
                if self._conver_uuid2hex(vid) in group_members:
                    self._leave_volume_group(volume, cgid)
        except Exception as e:
            msg = _("Fexvisor failed to remove the volume %(vol)s in the "
                    "group %(group)s due to "
                    "%(ret)s.") % {"vol": vid, "group": cgid,
                                   "ret": six.text_type(e)}
            raise exception.VolumeBackendAPIException(data=msg)
        return model_update, None, None

    def create_group(self, context, group):
        if volume_utils.is_group_a_cg_snapshot_type(group):
            return self._create_consistencygroup(context, group)
        raise NotImplementedError()

    def delete_group(self, context, group, volumes):
        if volume_utils.is_group_a_cg_snapshot_type(group):
            return self._delete_consistencygroup(context, group, volumes)
        raise NotImplementedError()

    def create_group_snapshot(self, context, group_snapshot, snapshots):
        if volume_utils.is_group_a_cg_snapshot_type(group_snapshot):
            return self._create_cgsnapshot(context, group_snapshot, snapshots)
        raise NotImplementedError()

    def delete_group_snapshot(self, context, group_snapshot, snapshots):
        if volume_utils.is_group_a_cg_snapshot_type(group_snapshot):
            return self._delete_cgsnapshot(context, group_snapshot, snapshots)
        raise NotImplementedError()

    def create_group_from_src(self, context, group, volumes,
                              group_snapshot=None, snapshots=None,
                              source_group=None, source_vols=None):
        err_msg = _("Prophet Storage doesn't support create_group_from_src.")
        LOG.error(err_msg)
        raise exception.VolumeBackendAPIException(data=err_msg)

    def create_volume(self, volume):
        """Create a volume."""
        pool = volume_utils.extract_host(volume['host'],
                                         level='pool')
        if not pool:
            if not self.configuration.dpl_pool:
                msg = _("Pool is not available in the volume host fields.")
                raise exception.InvalidHost(reason=msg)
            else:
                pool = self.configuration.dpl_pool

        ret, output = self.dpl.create_vdev(
            self._conver_uuid2hex(volume['id']),
            volume.get('display_name', ''),
            volume.get('display_description', ''),
            pool,
            int(volume['size']) * units.Gi,
            self.configuration.san_thin_provision)
        if ret == errno.EAGAIN:
            ret, event_uuid = self._get_event_uuid(output)
            if ret == 0:
                status = self._wait_event(self.dpl.get_vdev_status,
                                          volume['id'],
                                          event_uuid)
                if status['state'] != 'available':
                    msg = _('Flexvisor failed to create volume %(volume)s: '
                            '%(status)s.') % {'volume': volume['id'],
                                              'status': ret}
                    raise exception.VolumeBackendAPIException(data=msg)
            else:
                msg = _('Flexvisor failed to create volume (get event) '
                        '%s.') % (volume['id'])
                raise exception.VolumeBackendAPIException(
                    data=msg)
        elif ret != 0:
            msg = _('Flexvisor create volume failed.:%(volumeid)s:'
                    '%(status)s.') % {'volumeid': volume['id'],
                                      'status': ret}
            raise exception.VolumeBackendAPIException(
                data=msg)
        else:
            LOG.info('Flexvisor succeeded to create volume %(id)s.',
                     {'id': volume['id']})

        if volume.group_id:
            group = volume_utils.group_get_by_id(volume.group_id)
            if volume_utils.is_group_a_cg_snapshot_type(group):
                try:
                    self._join_volume_group(volume, volume.group_id)
                except Exception:
                    # Delete volume if volume failed to join group.
                    self.dpl.delete_vdev(self._conver_uuid2hex(volume['id']))
                    msg = _('Flexvisor failed to create volume %(id)s in the '
                            'group %(vgid)s.') % {
                        'id': volume['id'],
                        'vgid': volume.group_id}
                    raise exception.VolumeBackendAPIException(data=msg)

    def create_volume_from_snapshot(self, volume, snapshot):
        """Creates a volume from a snapshot."""
        src_volume = None
        vgID = None
        # Detect whether a member of the group.
        snapshotID = snapshot['id']
        # Try to get cgid if volume belong in the group.
        src_volumeID = snapshot['volume_id']
        cgsnapshotID = snapshot.get('group_snapshot_id', None)
        if cgsnapshotID:
            try:
                src_volume = self.db.volume_get(src_volumeID)
            except Exception:
                msg = _("Flexvisor unable to find the source volume "
                        "%(id)s info.") % {'id': src_volumeID}
                raise exception.VolumeBackendAPIException(data=msg)
        if src_volume:
            vgID = src_volume.group_id

        # Get the volume origin snapshot id if the source snapshot is group
        # snapshot.
        if vgID:
            snapshotID = self._get_snapshotid_of_vgsnapshot(
                self._conver_uuid2hex(vgID),
                self._conver_uuid2hex(cgsnapshotID),
                self._conver_uuid2hex(src_volumeID))

        pool = volume_utils.extract_host(volume['host'],
                                         level='pool')
        if not pool:
            if not self.configuration.dpl_pool:
                msg = _("Pool is not available in the volume host fields.")
                raise exception.InvalidHost(reason=msg)
            else:
                pool = self.configuration.dpl_pool

        ret, output = self.dpl.create_vdev_from_snapshot(
            self._conver_uuid2hex(volume['id']),
            volume.get('display_name', ''),
            volume.get('display_description', ''),
            self._conver_uuid2hex(snapshotID),
            pool,
            self.configuration.san_thin_provision)
        if ret == errno.EAGAIN:
            ret, event_uuid = self._get_event_uuid(output)
            if ret == 0:
                status = self._wait_event(self.dpl.get_vdev_status,
                                          volume['id'],
                                          event_uuid)
                if status['state'] != 'available':
                    msg = _('Flexvisor failed to create volume from '
                            'snapshot %(id)s:'
                            '%(status)s.') % {'id': snapshot['id'],
                                              'status': ret}
                    raise exception.VolumeBackendAPIException(
                        data=msg)
            else:
                msg = _('Flexvisor failed to create volume from snapshot '
                        '(failed to get event) '
                        '%(id)s.') % {'id': snapshot['id']}
                raise exception.VolumeBackendAPIException(data=msg)
        elif ret != 0:
            msg = _('Flexvisor failed to create volume from snapshot '
                    '%(id)s: %(status)s.') % {'id': snapshot['id'],
                                              'status': ret}
            raise exception.VolumeBackendAPIException(
                data=msg)
        else:
            LOG.info('Flexvisor succeeded to create volume %(id)s '
                     'from snapshot.', {'id': volume['id']})

        if volume['size'] > snapshot['volume_size']:
            self.extend_volume(volume, volume['size'])

        if volume.group_id:
            group = volume_utils.group_get_by_id(volume.group_id)
            if volume_utils.is_group_a_cg_snapshot_type(group):
                try:
                    self._join_volume_group(volume, volume.group_id)
                except Exception:
                    # Delete volume if volume failed to join group.
                    self.dpl.delete_vdev(self._conver_uuid2hex(volume['id']))
                    raise

    def spawn_volume_from_snapshot(self, volume, snapshot):
        """Spawn a REFERENCED volume from a snapshot."""
        ret, output = self.dpl.spawn_vdev_from_snapshot(
            self._conver_uuid2hex(volume['id']),
            self._conver_uuid2hex(snapshot['volume_id']),
            volume.get('display_name', ''),
            volume.get('display_description', ''),
            self._conver_uuid2hex(snapshot['id']))

        if ret == errno.EAGAIN:
            # its an async process
            ret, event_uuid = self._get_event_uuid(output)
            if ret == 0:
                status = self._wait_event(self.dpl.get_vdev_status,
                                          volume['id'], event_uuid)
                if status['state'] != 'available':
                    msg = _('Flexvisor failed to spawn volume from snapshot '
                            '%(id)s:%(status)s.') % {'id': snapshot['id'],
                                                     'status': ret}
                    raise exception.VolumeBackendAPIException(data=msg)
            else:
                msg = _('Flexvisor failed to spawn volume from snapshot '
                        '(failed to get event) '
                        '%(id)s.') % {'id': snapshot['id']}
                raise exception.VolumeBackendAPIException(data=msg)
        elif ret != 0:
            msg = _('Flexvisor failed to create volume from snapshot '
                    '%(id)s: %(status)s.') % {'id': snapshot['id'],
                                              'status': ret}

            raise exception.VolumeBackendAPIException(
                data=msg)
        else:
            LOG.info('Flexvisor succeeded to create volume %(id)s '
                     'from snapshot.', {'id': volume['id']})

    def create_cloned_volume(self, volume, src_vref):
        """Creates a clone of the specified volume."""
        pool = volume_utils.extract_host(volume['host'],
                                         level='pool')
        if not pool:
            if not self.configuration.dpl_pool:
                msg = _("Pool is not available in the volume host fields.")
                raise exception.InvalidHost(reason=msg)
            else:
                pool = self.configuration.dpl_pool

        ret, output = self.dpl.clone_vdev(
            self._conver_uuid2hex(src_vref['id']),
            self._conver_uuid2hex(volume['id']),
            pool,
            volume.get('display_name', ''),
            volume.get('display_description', ''),
            int(volume['size']) * units.Gi,
            self.configuration.san_thin_provision)
        if ret == errno.EAGAIN:
            ret, event_uuid = self._get_event_uuid(output)
            if ret == 0:
                status = self._wait_event(self.dpl.get_vdev_status,
                                          volume['id'],
                                          event_uuid)
                if status['state'] != 'available':
                    msg = _('Flexvisor failed to clone volume %(id)s: '
                            '%(status)s.') % {'id': src_vref['id'],
                                              'status': ret}
                    raise exception.VolumeBackendAPIException(data=msg)
            else:
                msg = _('Flexvisor failed to clone volume (failed to'
                        ' get event) %(id)s.') % {'id': src_vref['id']}
                raise exception.VolumeBackendAPIException(
                    data=msg)
        elif ret != 0:
            msg = _('Flexvisor failed to clone volume %(id)s: '
                    '%(status)s.') % {'id': src_vref['id'], 'status': ret}
            raise exception.VolumeBackendAPIException(
                data=msg)
        else:
            LOG.info('Flexvisor succeeded to clone volume %(id)s.',
                     {'id': volume['id']})

        if volume.group_id:
            group = volume_utils.group_get_by_id(volume.group_id)
            if volume_utils.is_group_a_cg_snapshot_type(group):
                try:
                    self._join_volume_group(volume, volume.group_id)
                except Exception:
                    # Delete volume if volume failed to join group.
                    self.dpl.delete_vdev(self._conver_uuid2hex(volume['id']))
                    msg = _('Flexvisor volume %(id)s failed to join group '
                            '%(vgid)s.') % {'id': volume['id'],
                                            'vgid': volume.group_id}
                    raise exception.VolumeBackendAPIException(data=msg)

    def delete_volume(self, volume):
        """Deletes a volume."""
        ret = 0
        if volume.group_id:
            group = volume_utils.group_get_by_id(volume.group_id)
            if group and volume_utils.is_group_a_cg_snapshot_type(group):
                msg = ''
                try:
                    ret, out = self.dpl.leave_vg(
                        self._conver_uuid2hex(volume['id']),
                        self._conver_uuid2hex(volume.group_id))
                    if ret:
                        LOG.warning('Flexvisor failed to delete volume '
                                    '%(id)s from the group %(vgid)s.',
                                    {'id': volume['id'],
                                     'vgid': volume.group_id})
                except Exception as e:
                    LOG.warning('Flexvisor failed to delete volume %(id)s '
                                'from group %(vgid)s due to %(status)s.',
                                {'id': volume['id'],
                                 'vgid': volume.group_id,
                                 'status': e})

            if ret:
                ret = 0

        ret, output = self.dpl.delete_vdev(self._conver_uuid2hex(volume['id']))
        if ret == errno.EAGAIN:
            status = self._wait_event(self.dpl.get_vdev, volume['id'])
            if status['state'] == 'error':
                msg = _('Flexvisor failed deleting volume %(id)s: '
                        '%(status)s.') % {'id': volume['id'], 'status': ret}
                raise exception.VolumeBackendAPIException(data=msg)
        elif ret == errno.ENODATA:
            ret = 0
            LOG.info('Flexvisor volume %(id)s does not '
                     'exist.', {'id': volume['id']})
        elif ret != 0:
            msg = _('Flexvisor failed to delete volume %(id)s: '
                    '%(status)s.') % {'id': volume['id'], 'status': ret}
            raise exception.VolumeBackendAPIException(
                data=msg)

    def extend_volume(self, volume, new_size):
        ret, output = self.dpl.extend_vdev(self._conver_uuid2hex(volume['id']),
                                           volume.get('display_name', ''),
                                           volume.get('display_description',
                                                      ''),
                                           new_size * units.Gi)
        if ret == errno.EAGAIN:
            ret, event_uuid = self._get_event_uuid(output)
            if ret == 0:
                status = self._wait_event(self.dpl.get_vdev_status,
                                          volume['id'],
                                          event_uuid)
                if status['state'] != 'available':
                    msg = _('Flexvisor failed to extend volume '
                            '%(id)s:%(status)s.') % {'id': volume,
                                                     'status': ret}
                    raise exception.VolumeBackendAPIException(
                        data=msg)
            else:
                msg = _('Flexvisor failed to extend volume '
                        '(failed to get event) '
                        '%(id)s.') % {'id': volume['id']}
                raise exception.VolumeBackendAPIException(data=msg)
        elif ret != 0:
            msg = _('Flexvisor failed to extend volume '
                    '%(id)s: %(status)s.') % {'id': volume['id'],
                                              'status': ret}
            raise exception.VolumeBackendAPIException(
                data=msg)
        else:
            LOG.info('Flexvisor succeeded to extend volume'
                     ' %(id)s.', {'id': volume['id']})

    def create_snapshot(self, snapshot):
        """Creates a snapshot."""
        ret, output = self.dpl.create_vdev_snapshot(
            self._conver_uuid2hex(snapshot['volume_id']),
            self._conver_uuid2hex(snapshot['id']),
            snapshot.get('display_name', ''),
            snapshot.get('display_description', ''))

        if ret == errno.EAGAIN:
            ret, event_uuid = self._get_event_uuid(output)
            if ret == 0:
                status = self._wait_event(self.dpl.get_vdev_status,
                                          snapshot['volume_id'],
                                          event_uuid)
                if status['state'] != 'available':
                    msg = (_('Flexvisor failed to create snapshot for volume '
                             '%(id)s: %(status)s.') %
                           {'id': snapshot['volume_id'], 'status': ret})
                    raise exception.VolumeBackendAPIException(data=msg)
            else:
                msg = (_('Flexvisor failed to create snapshot for volume '
                         '(failed to get event) %(id)s.') %
                       {'id': snapshot['volume_id']})
                raise exception.VolumeBackendAPIException(data=msg)
        elif ret != 0:
            msg = _('Flexvisor failed to create snapshot for volume %(id)s: '
                    '%(status)s.') % {'id': snapshot['volume_id'],
                                      'status': ret}
            raise exception.VolumeBackendAPIException(data=msg)

    def delete_snapshot(self, snapshot):
        """Deletes a snapshot."""
        ret, output = self.dpl.delete_vdev_snapshot(
            self._conver_uuid2hex(snapshot['volume_id']),
            self._conver_uuid2hex(snapshot['id']))
        if ret == errno.EAGAIN:
            ret, event_uuid = self._get_event_uuid(output)
            if ret == 0:
                status = self._wait_event(self.dpl.get_vdev_status,
                                          snapshot['volume_id'],
                                          event_uuid)
                if status['state'] != 'available':
                    msg = _('Flexvisor failed to delete snapshot %(id)s: '
                            '%(status)s.') % {'id': snapshot['id'],
                                              'status': ret}
                    raise exception.VolumeBackendAPIException(data=msg)
            else:
                msg = _('Flexvisor failed to delete snapshot (failed to '
                        'get event) %(id)s.') % {'id': snapshot['id']}
                raise exception.VolumeBackendAPIException(data=msg)
        elif ret == errno.ENODATA:
            LOG.info('Flexvisor snapshot %(id)s not existed.',
                     {'id': snapshot['id']})
        elif ret != 0:
            msg = _('Flexvisor failed to delete snapshot %(id)s: '
                    '%(status)s.') % {'id': snapshot['id'], 'status': ret}
            raise exception.VolumeBackendAPIException(data=msg)
        else:
            LOG.info('Flexvisor succeeded to delete snapshot %(id)s.',
                     {'id': snapshot['id']})

    def _get_pools(self):
        pools = []
        qpools = []
        # Defined access pool by cinder configuration.
        defined_pool = self.configuration.dpl_pool
        if defined_pool:
            qpools.append(defined_pool)
        else:
            try:
                ret, output = self.dpl.get_pools()
                if ret == 0:
                    for poolUuid, poolName in output.get('children', []):
                        qpools.append(poolUuid)
                else:
                    LOG.error("Flexvisor failed to get pool list."
                              " (Error: %d)", ret)
            except Exception as e:
                LOG.error("Flexvisor failed to get pool list due to "
                          "%s.", e)

        # Query pool detail information
        for poolid in qpools:
            ret, output = self._get_pool_info(poolid)
            if ret == 0:
                pool = {}
                pool['pool_name'] = output['metadata']['pool_uuid']
                pool['total_capacity_gb'] = (
                    self._convert_size_GB(
                        int(output['metadata']['total_capacity'])))
                pool['free_capacity_gb'] = (
                    self._convert_size_GB(
                        int(output['metadata']['available_capacity'])))
                pool['QoS_support'] = False
                pool['reserved_percentage'] = 0
                pools.append(pool)
            else:
                LOG.warning("Failed to query pool %(id)s status "
                            "%(ret)d.", {'id': poolid, 'ret': ret})
                continue
        return pools

    def _update_volume_stats(self, refresh=False):
        """Return the current state of the volume service.

        If 'refresh' is True, run the update first.
        """
        data = {}
        pools = self._get_pools()
        data['volume_backend_name'] = (
            self.configuration.safe_get('volume_backend_name'))
        location_info = '%(driver)s:%(host)s:%(volume)s' % {
            'driver': self.__class__.__name__,
            'host': self.configuration.san_ip,
            'volume': self.configuration.dpl_pool
        }
        try:
            ret, output = self.dpl.get_server_info()
            if ret == 0:
                data['vendor_name'] = output['metadata']['vendor']
                data['driver_version'] = output['metadata']['version']
                data['storage_protocol'] = constants.ISCSI
                data['location_info'] = location_info
                data['consistencygroup_support'] = True
                data['consistent_group_snapshot_enabled'] = True
                data['pools'] = pools
                self._stats = data
        except Exception as e:
            LOG.error('Failed to get server info due to '
                      '%(state)s.', {'state': e})
        return self._stats

    def do_setup(self, context):
        """Any initialization the volume driver does while starting."""
        self.context = context
        LOG.info('Activate Flexvisor cinder volume driver.')

    def check_for_setup_error(self):
        """Check DPL can connect properly."""
        pass

    def _get_pool_info(self, poolid):
        """Query pool information."""
        ret, output = self.dpl.get_pool(poolid)
        if ret == errno.EAGAIN:
            ret, event_uuid = self._get_event_uuid(output)
            if ret == 0:
                status = self._wait_event(self.dpl.get_pool_status, poolid,
                                          event_uuid)
                if status['state'] != 'available':
                    msg = _('Flexvisor failed to get pool info %(id)s: '
                            '%(status)s.') % {'id': poolid, 'status': ret}
                    raise exception.VolumeBackendAPIException(data=msg)
                else:
                    ret = 0
                    output = status.get('output', {})
            else:
                LOG.error('Flexvisor failed to get pool %(id)s info.',
                          {'id': poolid})
                raise exception.VolumeBackendAPIException(
                    data="failed to get event")
        elif ret != 0:
            msg = _('Flexvisor failed to get pool info %(id)s: '
                    '%(status)s.') % {'id': poolid, 'status': ret}
            raise exception.VolumeBackendAPIException(data=msg)
        else:
            LOG.debug('Flexvisor succeeded to get pool info.')
        return ret, output
