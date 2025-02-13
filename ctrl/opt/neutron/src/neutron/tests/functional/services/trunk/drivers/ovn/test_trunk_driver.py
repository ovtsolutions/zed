# Copyright 2017 DT Dream Technology Co.,Ltd.
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

import contextlib

from neutron_lib.api.definitions import portbindings
from neutron_lib.callbacks import exceptions as n_exc
from neutron_lib import constants as n_consts
from neutron_lib.db import api as db_api
from neutron_lib.plugins import utils
from neutron_lib.services.trunk import constants as trunk_consts
from oslo_utils import uuidutils

from neutron.common.ovn import constants as ovn_const
from neutron.objects import ports as port_obj
from neutron.services.trunk import plugin as trunk_plugin
from neutron.tests.functional import base


class TestOVNTrunkDriver(base.TestOVNFunctionalBase):

    def setUp(self, **kwargs):
        super().setUp(**kwargs)
        self.trunk_plugin = trunk_plugin.TrunkPlugin()
        self.trunk_plugin.add_segmentation_type(
            trunk_consts.SEGMENTATION_TYPE_VLAN,
            utils.is_valid_vlan_tag)

    @contextlib.contextmanager
    def trunk(self, sub_ports=None):
        sub_ports = sub_ports or []
        with self.network() as network:
            with self.subnet(network=network) as subnet:
                with self.port(subnet=subnet,
                               device_owner='compute:nova') as parent_port:
                    tenant_id = uuidutils.generate_uuid()
                    trunk = {'trunk': {
                        'port_id': parent_port['port']['id'],
                        'tenant_id': tenant_id, 'project_id': tenant_id,
                        'admin_state_up': True,
                        'name': 'trunk', 'sub_ports': sub_ports}}
                    trunk = self.trunk_plugin.create_trunk(self.context, trunk)
                    yield trunk

    @contextlib.contextmanager
    def subport(self):
        with self.port() as port:
            sub_port = {'segmentation_type': 'vlan',
                        'segmentation_id': 1000,
                        'port_id': port['port']['id']}
            yield sub_port

    def _get_ovn_trunk_info(self):
        ovn_trunk_info = []
        for row in self.nb_api.tables[
                'Logical_Switch_Port'].rows.values():
            if row.parent_name and row.tag:
                device_owner = row.external_ids[
                    ovn_const.OVN_DEVICE_OWNER_EXT_ID_KEY]
                ovn_trunk_info.append({'port_id': row.name,
                                       'parent_port_id': row.parent_name,
                                       'tag': row.tag,
                                       'device_owner': device_owner,
                                       })
        return ovn_trunk_info

    def _verify_trunk_info(self, trunk, has_items, host=''):
        ovn_subports_info = self._get_ovn_trunk_info()
        neutron_subports_info = []
        for subport in trunk.get('sub_ports', []):
            neutron_subports_info.append(
                {'port_id': subport['port_id'],
                 'parent_port_id': [trunk['port_id']],
                 'tag': [subport['segmentation_id']],
                 'device_owner': trunk_consts.TRUNK_SUBPORT_OWNER,
                 })
            # Check the subport binding.
            pb = port_obj.PortBinding.get_object(
                self.context, port_id=subport['port_id'], host=host)
            self.assertEqual(n_consts.PORT_STATUS_ACTIVE, pb.status)
            self.assertEqual(host, pb.host)

        self.assertCountEqual(ovn_subports_info, neutron_subports_info)
        self.assertEqual(has_items, len(neutron_subports_info) != 0)

        if trunk.get('status'):
            self.assertEqual(trunk_consts.TRUNK_ACTIVE_STATUS, trunk['status'])

    def _bind_port(self, port_id, host):
        with db_api.CONTEXT_WRITER.using(self.context):
            pb = port_obj.PortBinding.get_object(self.context,
                                                 port_id=port_id, host='')
            pb.delete()
            port_obj.PortBinding(self.context, port_id=port_id, host=host,
                                 vif_type=portbindings.VIF_TYPE_OVS).create()

    def test_trunk_create(self):
        with self.trunk() as trunk:
            self._verify_trunk_info(trunk, has_items=False)

    def test_trunk_create_with_subports(self):
        with self.subport() as subport:
            with self.trunk([subport]) as trunk:
                self._verify_trunk_info(trunk, has_items=True)

    def test_trunk_create_parent_port_bound(self):
        with self.network() as network:
            with self.subnet(network=network) as subnet:
                with self.port(subnet=subnet) as parent_port:
                    pb = port_obj.PortBinding.get_objects(
                        self.context, port_id=parent_port['port']['id'])
                    port_obj.PortBinding.update_object(
                        self.context, {'vif_type': portbindings.VIF_TYPE_OVS},
                        port_id=pb[0].port_id, host=pb[0].host)
                    tenant_id = uuidutils.generate_uuid()
                    trunk = {'trunk': {
                        'port_id': parent_port['port']['id'],
                        'tenant_id': tenant_id, 'project_id': tenant_id,
                        'admin_state_up': True,
                        'name': 'trunk', 'sub_ports': []}}
                    self.assertRaises(n_exc.CallbackFailure,
                                      self.trunk_plugin.create_trunk,
                                      self.context, trunk)

    def test_subport_add(self):
        with self.subport() as subport:
            with self.trunk() as trunk:
                self.trunk_plugin.add_subports(self.context, trunk['id'],
                                               {'sub_ports': [subport]})
                new_trunk = self.trunk_plugin.get_trunk(self.context,
                                                        trunk['id'])
                self._verify_trunk_info(new_trunk, has_items=True)
                # Bind parent port. That will trigger the binding of the
                # trunk subports too, using the same host ID.
                self._bind_port(trunk['port_id'], 'host1')
                self.mech_driver.set_port_status_up(trunk['port_id'])
                self._verify_trunk_info(new_trunk, has_items=True,
                                        host='host1')

    def test_subport_delete(self):
        with self.subport() as subport:
            with self.trunk([subport]) as trunk:
                # Bind parent port.
                self._bind_port(trunk['port_id'], 'host1')
                self.mech_driver.set_port_status_up(trunk['port_id'])
                self._verify_trunk_info(trunk, has_items=True,
                                        host='host1')

                self.trunk_plugin.remove_subports(self.context, trunk['id'],
                                                  {'sub_ports': [subport]})
                new_trunk = self.trunk_plugin.get_trunk(self.context,
                                                        trunk['id'])
                self._verify_trunk_info(new_trunk, has_items=False)

    def test_trunk_delete(self):
        with self.trunk() as trunk:
            self.trunk_plugin.delete_trunk(self.context, trunk['id'])
            self._verify_trunk_info({}, has_items=False)

    def test_trunk_delete_parent_port_bound(self):
        with self.trunk() as trunk:
            bp = port_obj.PortBinding.get_objects(
                self.context, port_id=trunk['port_id'])
            port_obj.PortBinding.update_object(
                self.context, {'vif_type': portbindings.VIF_TYPE_OVS},
                port_id=bp[0].port_id, host=bp[0].host)
            self.assertRaises(n_exc.CallbackFailure,
                              self.trunk_plugin.delete_trunk,
                              self.context, trunk['id'])
