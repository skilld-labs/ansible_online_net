#!/usr/bin/python
# -*- coding: utf-8 -*-
######################################################################
# (c) 2015, Jean-Baptiste Guerraz <jbguerraz@gmail.com>,
#           Andrey Postnikov <apostnikov@gmail.com>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

DOCUMENTATION = '''
---
module: online_net
short_description: Manage an Online.net server
description:
     - 
version_added: "1.0"
options:
  api_uri:
    description:
     - String, Online.net API URI
    default: 'https://api.online.net/api/v1/'
  api_token:
    description:
     - String, Online.net API token.
    required: true
  state:
    description:
     - Indicate desired state of the server.
    choices: ['on', 'off', 'reboot']
  boot_mode:
    description:
     - String, set to 'rescue-[RESCUE_IMAGE]' in order to reboot the server in rescue mode; set to 'normal' (default) for a simple reboot
  rescue_images:
     - Boolean, set to True in order to get the list of rescue images
  id:
    description:
     - Numeric, the server id you want to operate on.
    required: true
  hostname:
    description:
     - String, this is the host name of the server - must be formatted by hostname rules.
  rpn_groups:
    description:
     - List, the Online.net RPN groups to have the server part of.
  bmc:
     - String, the IP address to authorize for the BMC session
  bmc_close:
     - String, the key of the BMC session to close

notes:
  - Two environment variables can be used, ONLINE_NET_API_URI and ONLINE_NET_API_TOKEN.
  - As of Ansible 2.0, Version 1 of the Online.net API is used.
requirements:
  - "python >= 2.6"
'''


EXAMPLES = '''
# Reboot a server
# Reboot the given server (id=1337)

- online_net: >
      id=1337
      state='reboot'

# Add a server to few RPN groups
# Add the given server to the given group
# If the group doesn't exists yet, it'll be auto-created

- online_net: >
      id=1337
      rpn_groups=ThePrivateGroup,TheOtherPrivateGroup
      state='reboot'
'''

try:
    import json
except ImportError:
    import simplejson as json

has_http_lib = True
try:
    import httplib2
except ImportError:
    has_http_lib = False

from urllib import urlencode


class JsonfyMixIn(object):
    def to_json(self):
        return self.__dict__


class Server(JsonfyMixIn):
    def __init__(self, server_json):
        self.changed = False
        self.rescue_image = False
        self.__dict__.update(server_json)

    def has_changed(self):
        return self.changed

    def state(self, state):
        if state == 'on':
            if self.power == 'OFF':
                if self.api('server/boot/normal/' + str(self.id), dict(reason='Started by Ansible plugin')):
                    self.power = 'ON'
                    self.changed = True
                    return True
                else:
                    return False
            else:
                return False
        elif state == 'off':
            if self.power == 'ON':
                if self.api('server/shutdown/' + str(self.id), dict(reason='Shutted down by Ansible plugin')):
                    self.power = 'OFF'
                    self.changed = True
                    return True
                else:
                    return False
            else:
                return False
        elif state == 'reboot':
            if 'rescue' in self.boot_mode:
                self.changed = self.api('server/boot/rescue/' + str(self.id), dict(image=self.boot_mode.replace('rescue-', '', 1)))
                return self.changed
            else:
                self.changed = self.api('server/reboot/' + str(self.id), dict(reason='Rebooted by Ansible plugin'))
                return self.changed
        else:
            return False

    def name(self, name):
        if self.api('server/' + str(self.id), dict(hostname=name), 'PUT'):
            self.hostname = name
            self.changed = True
            return True
        else:
            return False

    def rpn_groups(self, join_groups):
        groups = self.api('rpn/group')

        groups_names_to_ids = {}
        server_groups = []

        for group in groups:
            groups_names_to_ids[group['name']] = group['id']
            for member in group['members']:
                if self.id == member['id']:
                    server_groups.append(group['id'])
                    break

        for group_name in join_groups:
            if group_name not in groups_names_to_ids:
                self.api('rpn/group', dict(name=group_name, server_ids=self.id)) 

        # heh. lazy sync !
        sync_success = True
        for group_id in server_groups:
            if not self.api('rpn/group/removeServers', dict(group_id=group_id, server_ids=self.id)):
                sync_success = False
        server_groups = []
        for group_name in join_groups:
            if not self.api('rpn/group/addServers', dict(group_id=groups_names_to_ids[group_name], server_ids=self.id)):
                sync_success = False
            else:
                server_groups.append(dict(id=groups_names_to_ids[group_name], name=group_name))
        self.groups = server_groups

        return sync_success

    def rescue_images(self):
        return self.api('server/rescue_images/' + str(self.id))

    def _bmc(self, ip):
        session_key =  self.api('server/bmc/session', dict(server_id=self.id, ip=ip))
        if session_key:
          authentication = False
          while not authentication:
              authentication = self.api('server/bmc/session/' + session_key)
              if authentication:
                  authentication['session_key'] = session_key
                  break
              else:
                  time.sleep(1)
          self.changed = True
          return authentication
        else:
          return False

    def bmc_close(self, session_key):
        self.bmc['session_key'] = None
        return self.api('server/bmc/session/' + str(session_key), dict(bmc='close'), 'DELETE')

    @classmethod
    def find(cls, server_id=None):
        if not server_id:
            return False
        server_json = cls.api('server/' + str(server_id))
        if not server_json:
            return False
        else:
            return Server(server_json)

    @classmethod
    def setup(cls, api_uri, api_token):
        cls.api_uri = api_uri
        cls.api_token = api_token

    @classmethod
    def api(cls, command='server', parameters=None, method='POST'):
        # Create a Http object and set some default options.
        h = httplib2.Http(disable_ssl_certificate_validation=True, timeout=30)

        headers = {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + cls.api_token,
        }

        if parameters:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
            resp, content = h.request(cls.api_uri + command, method, urlencode(parameters), headers=headers)
        else:
            resp, content = h.request(cls.api_uri + command, headers=headers)

        resp['status'] = int(resp['status'])
        if resp['status'] in range(200,204):
            return json.loads(unicode(content.decode('raw_unicode_escape')))
        else:
            return None


def core(module):

    try:
        api_uri = module.params['api_uri'] or os.environ['ONLINE_NET_API_URI']
        api_token = module.params['api_token'] or os.environ['ONLINE_NET_API_TOKEN']
        server_id = module.params['id']
    except KeyError, e:
        module.fail_json(msg='Unable to load %s' % e.message)

    state = module.params['state']
    boot_mode = module.params['boot_mode']
    hostname = module.params['hostname']
    rpn_groups = module.params['rpn_groups']
    rescue_images = module.params['rescue_images']
    bmc = module.params['bmc']
    bmc_close = module.params['bmc_close']

    # First, try to find a server by id.
    Server.setup(api_uri, api_token)
    server = Server.find(server_id)

    # If we couldn't find the server, exit
    if not server:
        module.fail_json(msg='Unable to find the server %s' % server_id)
    else:
        output = []

        if hostname:
            output.append({'hostname': server.name(hostname)})

        if rpn_groups:
            output.append({'rpn_groups': server.rpn_groups(rpn_groups)})

        if rescue_images:
            output.append({'rescue_images': server.rescue_images()})

        if bmc:
            output.append({'bmc': server._bmc(bmc)})

        if bmc_close:
            output.append({'bmc_close': server.bmc_close(bmc_close)})

        if boot_mode:
            server.boot_mode = boot_mode

        if state:
            output.append({'state': server.state(state)})

        module.exit_json(changed=server.has_changed(), server=server.to_json(), output=json.dumps(output))


def main():
    module = AnsibleModule(
        argument_spec=dict(
            api_uri=dict(aliases=['API_URI'], default='https://api.online.net/api/v1/', no_log=True),
            api_token=dict(aliases=['API_TOKEN'], no_log=True, required=True),
            id=dict(alias=['server_id'], type='int', required=True),
            state=dict(choices=['on', 'off', 'reboot']),
            boot_mode=dict(type='str', default='normal'),
            hostname=dict(type='str'),
            rpn_groups=dict(type='list'),
            rescue_images=dict(type='bool', default='no'),
            bmc=dict(type='str'),
            bmc_close=dict(type='str')
        )
    )

    if not has_http_lib:
        module.fail_json(msg='`httplib2` library required for this module')

    try:
        core(module)
    except Exception, e:
        module.fail_json(msg=str(e))

# import module snippets
from ansible.module_utils.basic import *

if __name__ == '__main__':
    main()
