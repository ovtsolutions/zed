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


from oslo_policy import policy

from placement.policies import base


PREFIX = 'placement:reshaper:%s'
RESHAPE = PREFIX % 'reshape'

rules = [
    policy.DocumentedRuleDefault(
        RESHAPE,
        base.SYSTEM_ADMIN,
        "Reshape Inventory and Allocations.",
        [
            {
                'method': 'POST',
                'path': '/reshaper'
            }
        ],
        scope_types=['system'],
    ),
]


def list_rules():
    return rules
