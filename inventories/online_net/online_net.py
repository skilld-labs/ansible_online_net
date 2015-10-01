#!/usr/bin/env python

'''
Online.net external inventory script
======================================

Generates Ansible inventory of Online.net servers.

The --pretty (-p) option pretty-prints the output for better human readability.

----
Although the cache stores all the information received from Online.net,
the cache is not used for current server information (in --list, --host, --all).
This is so that accurate server information is always found.
You can force this script to use the cache with --force-cache.

----
Configuration is read from `online_net.ini`, then from environment variables,
then and command-line arguments.

Most notably, the Online.net API base uri and token must be specified.  They
can be specified in the INI file or with the following environment variables:
    export ONLINE_NET_API_URI='https://api.online.net/api/v1/'
    export ONLINE_NET_API_TOKEN='abc123'

Alternatively, they can be passed on the command-line with --api-uri and
--api-token.

If you specify Online.net information in the INI file, a handy way to
get them into your environment (e.g., to use the online_net module)
is to use the output of the --env option with export:
    export $(online_net.py --env)

----
The following groups are generated from --list:
 - ID        (server ID)
 - hostname  (server hostname)
 - os
 - datacenter

When run against a specific host, this script returns the following variables:
 - private ip address
 - public ip address
 - datacenter
 - os
 - ...

-----
```
usage: online_net.py [-h] [--list] [--host HOST] [--all]
                                 [--pretty]
                                 [--cache-path CACHE_PATH]
                                 [--cache-max_age CACHE_MAX_AGE]
                                 [--refresh-cache]
                                 [--api-uri API_URI]
                                 [--api-token API_TOKEN]

Produce an Ansible Inventory file based on Online.net api

optional arguments:
  -h, --help            show this help message and exit
  --list                List all Online.net servers as Ansible inventory
                        (default: True)
  --host HOST           Get all Ansible inventory variables about a specific
                        server
  --all                 List all Online.net information as JSON
  --pretty, -p          Pretty-print results
  --cache-path CACHE_PATH
                        Path to the cache files (default: .)
  --cache-max_age CACHE_MAX_AGE
                        Maximum age of the cached items (default: 0)
  --refresh-cache       Force refresh of cache by making API requests to
                        Online.net (default: False - use cache files)
  --api-uri API_URI, -u 
                        Online.net API URI
  --api-token API_TOKEN, -a API_TOKEN
                        Online.net API token
```

'''
######################################################################

# (c) 2015, Jean-Baptiste Guerraz <jbguerraz@gmail.com>,
#           Andrey Postnikov <apostnikov@gmail.com>
#
# Inspired by the DigitalOcean inventory plugin:
# https://github.com/ansible/ansible/blob/devel/plugins/inventory/digital_ocean.py
#
# This file is part of Ansible,
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

######################################################################

import os
import sys
import re
import argparse
from time import time

try:
    import json
except ImportError:
    import simplejson as json

try:
    import httplib2
except ImportError:
    print "failed=True msg='`httplib2` library required for this script'"
    sys.exit(1)

try:
    import six
    from six.moves import configparser
except ImportError, e:
    print "failed=True msg='`six` library required for this script'"
    sys.exit(1)


class OnlineNetInventory(object):

    ###########################################################################
    # Main execution path
    ###########################################################################

    def __init__(self):
        # Main execution path

        # OnlineNetInventory data
        self.data = {}       # All Online.net data
        self.inventory = {}  # Ansible Inventory
        self.index = {}      # Various indices of servers metadata

        # Define defaults
        self.api_uri = 'https://api.online.net/api/v1/'
        self.api_token = None
        self.cache_path = '.'
        self.cache_max_age = 0

        # Read settings, environment variables, and CLI arguments
        self.read_settings()
        self.read_environment()
        self.args = self.read_cli_args()

        # Verify API information were set
        if self.api_token is None:
            print '''Could not find values for Online.net api_token.
That must be specified via either ini file (default, or custom using the environment variable ONLINE_NET_INI_PATH),
command line argument (--api-token), or environment variable (ONLINE_NET_API_TOKEN)'''
            sys.exit(-1)

        # env command, show Online.net details
        if self.args.env:
            print 'ONLINE_NET_API_URI=%s ONLINE_NET_API_TOKEN=%s' % (self.api_uri, self.api_token)
            sys.exit(0)

        # Manage cache
        self.cache_filename = self.cache_path + '/ansible-online_net.cache'

        if not self.args.force_cache and self.args.refresh_cache or not self.is_cache_valid():
            self.load_from_online_net()
        else:
            self.load_from_cache()
            if len(self.data) == 0:
                if self.args.force_cache:
                    print 'Cache is empty and --force-cache was specified'
                    sys.exit(-1)
                self.load_from_online_net()
            else:
                # We always get fresh servers for --list, --host, and --all
                # unless --force-cache is specified
                if not self.args.force_cache and (
                   self.args.list or self.args.host or self.args.all):
                    self.load_from_online_net()

        # Pick the json_data to print based on the CLI command
        if self.args.all:
            json_data = self.data

        elif self.args.host:
            json_data = self.load_variables_for_host()

        else:
            # '--list' this is last to make it default
            json_data = self.inventory

        if self.args.pretty:
            print json.dumps(json_data, sort_keys=True, indent=2)
        else:
            print json.dumps(json_data)
        ''' That's all she wrote...Goodnight, it's over with, that's all she wrote '''

    ###########################################################################
    # Script configuration
    ###########################################################################

    def read_settings(self):
        # Reads the settings from the online_net.ini file
        if six.PY2:
            config = configparser.SafeConfigParser()
        else:
            config = configparser.ConfigParser()
        default_ini_path = os.path.dirname(os.path.realpath(__file__)) + '/online_net.ini'
        ini_path = os.environ.get('ONLINE_NET_INI_PATH', default_ini_path)
        config.read(ini_path)

        # API
        if config.has_option('online_net', 'api_uri'):
            self.api_uri = config.get('online_net', 'api_uri')

        if config.has_option('online_net', 'api_token'):
            self.api_token = config.get('online_net', 'api_token')

        # Cache related
        if config.has_option('online_net', 'cache_path'):
            self.cache_path = config.get('online_net', 'cache_path')
        if config.has_option('online_net', 'cache_max_age'):
            self.cache_max_age = config.getint('online_net', 'cache_max_age')

    def read_environment(self):
        # Reads the settings from environment variables
        # API
        if os.getenv('ONLINE_NET_API_URI'):
            self.api_uri = os.getenv('ONLINE_NET_API_URI')
        if os.getenv('ONLINE_NET_API_TOKEN'):
            self.api_token = os.getenv('ONLINE_NET_API_TOKEN')

    def read_cli_args(self):
        # Command line argument processing
        parser = argparse.ArgumentParser(description='Produce an Ansible Inventory file based on Online.net API')
        parser.add_argument('--list', action='store_true', help='List all Online.net servers as Ansible inventory (default: True)')
        parser.add_argument('--host', action='store', help='Get all Ansible inventory variables about a specific server')
        parser.add_argument('--all', action='store_true', help='List all Online.net information as RAW JSON')

        parser.add_argument('--pretty', '-p', action='store_true', help='Pretty-print results')

        parser.add_argument('--cache-path', action='store', help='Path to the cache files (default: .)')
        parser.add_argument('--cache-max_age', action='store', help='Maximum age of the cached items (default: 0)')
        parser.add_argument('--force-cache', action='store_true', default=True, help='Only use data from the cache')
        parser.add_argument('--refresh-cache', '-r', action='store_true', default=False, help='Force refresh of cache by making API requests to Online.net (default: False - use cache files)')

        parser.add_argument('--env', '-e', action='store_true', help='Display ONLINE_NET_API_URI and ONLINE_NET_API_TOKEN')
        parser.add_argument('--api-uri', '-u', action='store', help='Online.net API URI')
        parser.add_argument('--api-token', '-t', action='store', help='Online.net API token')

        args = parser.parse_args()

        if args.api_uri:
            self.api_uri = args.api_uri
        if args.api_token:
            self.api_token = args.api_token
        if args.cache_path:
            self.cache_path = args.cache_path
        if args.cache_max_age:
            self.cache_max_age = args.cache_max_age

        # Make --list default if none of the other commands are specified
        if not args.all and not args.host:
            args.list = True

        return args

    ###########################################################################
    # Data Management
    ###########################################################################

    def load_from_online_net(self):
        # Use Online.net API to get all the information from Online.net and save data in cache files

        servers = []
        servers_uris = self.api()

        for server_uri in servers_uris:
            servers.append(self.api('server/' + server_uri.rsplit('/', 1)[1]))
        
        self.data = servers

        self.index['host_to_server'] = self.build_index(self.data, 'network.ip')
        self.index['id_to_server'] = self.build_index(self.data, 'id')
        self.index['os_to_servers'] = self.build_index(self.data, 'os.name')
        self.index['dc_to_servers'] = self.build_index(self.data, 'location.datacenter')

        self.build_inventory()

        self.write_to_cache()

    def build_index(self, data, index_key):
        index = {}

        for idx in enumerate(data):
            if 'network.ip' == index_key:
                key = idx[1]['network']['ip'][0]
            elif 'id' == index_key:
                key = idx[1]['id']
            elif 'os.name' == index_key:
                key = idx[1]['os']['name']
            elif 'location.datacenter' == index_key:
                key = idx[1]['location']['datacenter']
            else:
                key = None
            if key is not None:
                self.push(index, str(key), idx[0])

        return index

    def build_inventory(self):
        # Build Ansible inventory of servers
        # Fist empty the inventory (could be set by cache) and then add all servers by id, hostname, os and datacenter

        self.inventory = {}

        for server in self.data:
            dest = server['network']['ip'][0]

            self.inventory['id_' + str(server['id'])] = [dest]
            self.push(self.inventory, server['hostname'], dest)
            self.push(self.inventory, 'os_' + server['os']['name'], dest)
            self.push(self.inventory, 'dc_' + server['location']['datacenter'], dest)

    def load_variables_for_host(self):
        # Generate a JSON response to a --host call
        host = self.to_safe(str(self.args.host))

        if host in self.index['host_to_server']:
            server = self.index['host_to_server'][host][0]
        elif host in self.index['id_to_server']:
            server = self.index['id_to_server'][host][0]
        else:
            return {}
          
        server = self.data[server]
        if not server:
            return {}

        # Put all the information in a 'online_net_' namespace
        info = {}
        for k, v in server.items():
            info['online_net_' + k] = v

        return info

    ###########################################################################
    # Cache Management
    ###########################################################################

    def is_cache_valid(self):
        # Determines if the cache files have expired, or if it is still valid
        if os.path.isfile(self.cache_filename):
            mod_time = os.path.getmtime(self.cache_filename)
            current_time = time()
            if (mod_time + self.cache_max_age) > current_time:
                return True
        return False

    def load_from_cache(self):
        # Reads the data from the cache file and assigns it to member variables as Python Objects
        cache = open(self.cache_filename, 'r')
        json_data = cache.read()
        cache.close()
        data = json.loads(json_data)

        self.data = data['data']
        self.inventory = data['inventory']
        self.index = data['index']

    def write_to_cache(self):
        # Writes data in JSON format to a file
        data = {'data': self.data, 'index': self.index, 'inventory': self.inventory}
        json_data = json.dumps(data, sort_keys=True, indent=2)

        cache = open(self.cache_filename, 'w')
        cache.write(json_data)
        cache.close()

    ###########################################################################
    # Utilities
    ###########################################################################

    @staticmethod
    def push(my_dict, key, element):
        # Pushed an element onto an array that may not have been defined in the dict
        if key in my_dict:
            my_dict[key].append(element)
        else:
            my_dict[key] = [element]

    @staticmethod
    def to_safe(word):
        # Converts 'bad' characters in a string to underscores so they can be used as Ansible groups
        return re.sub('[^A-Za-z0-9\-\.]', '_', word)

    def sanitize_dict(self, d):
        new_dict = {}
        for k, v in d.items():
            if v is not None:
                new_dict[self.to_safe(str(k))] = self.to_safe(str(v))
        return new_dict

    def sanitize_list(self, seq):
        new_seq = []
        for d in seq:
            new_seq.append(self.sanitize_dict(d))
        return new_seq

    def api(self, command='server'):
        # Create a Http object and set some default options.
        h = httplib2.Http(disable_ssl_certificate_validation=True, timeout=30)
        headers = {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + self.api_token,
        }
        resp, content = h.request(self.api_uri + command, headers=headers)
        resp['status'] = int(resp['status'])
        if resp['status'] == 200:
            return json.loads(unicode(content.decode('raw_unicode_escape')))
        else:
            return {}

###########################################################################
# Run the script
###########################################################################

OnlineNetInventory()
