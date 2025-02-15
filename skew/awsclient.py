# Copyright 2015 Mitch Garnaat
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import time

import datetime
import jmespath
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from skew.config import get_config

LOG = logging.getLogger(__name__)


def json_encoder(obj):
    """JSON encoder that formats datetimes as ISO8601 format."""
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    else:
        return obj


class AWSClient(object):

    def __init__(self, service_name, region_name, account_id, **kwargs):
        if 'config' in kwargs and kwargs['config'] is not None:
            self._config = kwargs['config']
        else:
            self._config = get_config()
        self._service_name = service_name
        self._region_name = region_name
        self._account_id = account_id
        self._has_credentials = False
        self.aws_creds = kwargs.get('aws_creds')
        self._profile = None
        self._external_id = None
        self._role_arn = None
        if self.aws_creds is None:
            self.aws_creds = self._config['accounts'][account_id].get(
                'credentials')
        if self.aws_creds is None:
            # no aws_creds, need profile to get creds from ~/.aws/credentials
            if 'profile' in self._config['accounts'][account_id]:
                self._profile = self._config['accounts'][account_id]['profile']
            elif 'role_arn' in self._config['accounts'][account_id]:
                self._role_arn = self._config['accounts'][account_id]['role_arn']
                if 'external_id' in self._config['accounts'][account_id]:
                    self._external_id = self._config['accounts'][account_id]['external_id']
        self.placebo = kwargs.get('placebo')
        self.placebo_dir = kwargs.get('placebo_dir')
        self.placebo_mode = kwargs.get('placebo_mode', 'record')
        self._client = self._create_client()

    @property
    def service_name(self):
        return self._service_name

    @property
    def region_name(self):
        return self._region_name

    @property
    def account_id(self):
        return self._account_id

    @property
    def profile(self):
        return self._profile

    @property
    def external_id(self):
        return self._external_id

    @property
    def role_arn(self):
        return self._role_arn

    def _create_client(self):
        if self.aws_creds:
            session = boto3.Session(**self.aws_creds)
        elif self.profile:
            session = boto3.Session(
                profile_name=self.profile)
        else:
            # we assumpe that somewhere, somehow credentials are set and boto3 will use them
            sts = boto3.client('sts')
            if self.external_id:
                assumed_role = sts.assume_role(
                    RoleArn=self.role_arn,
                    RoleSessionName='skew',
                    ExternalId=self.external_id
                )
            else:
                assumed_role = sts.assume_role(
                    RoleArn=self.role_arn,
                    RoleSessionName='skew'
                )
            session = boto3.Session(
                aws_access_key_id=assumed_role['Credentials']['AccessKeyId'],
                aws_secret_access_key=assumed_role['Credentials']['SecretAccessKey'],
                aws_session_token=assumed_role['Credentials']['SessionToken']
                )
        if self.placebo and self.placebo_dir:
            pill = self.placebo.attach(session, self.placebo_dir)
            if self.placebo_mode == 'record':
                pill.record()
            elif self.placebo_mode == 'playback':
                pill.playback()
        return session.client(self.service_name,
                              config=Config(connect_timeout=5, read_timeout=60, retries={'max_attempts': 30, 'mode': 'standard'}),
                              region_name=self.region_name if self.region_name else None)

    def call(self, op_name, query=None, **kwargs):
        """
        Make a request to a method in this client.  The response data is
        returned from this call as native Python data structures.

        This method differs from just calling the client method directly
        in the following ways:

          * It automatically handles the pagination rather than
            relying on a separate pagination method call.
          * You can pass an optional jmespath query and this query
            will be applied to the data returned from the low-level
            call.  This allows you to tailor the returned data to be
            exactly what you want.

        :type op_name: str
        :param op_name: The name of the request you wish to make.

        :type query: str
        :param query: A jmespath query that will be applied to the
            data returned by the operation prior to returning
            it to the user.

        :type kwargs: keyword arguments
        :param kwargs: Additional keyword arguments you want to pass
            to the method when making the request.
        """
        LOG.debug(kwargs)
        if query:
            query = jmespath.compile(query)
        if self._client.can_paginate(op_name):
            paginator = self._client.get_paginator(op_name)
            results = paginator.paginate(**kwargs)
            data = results.build_full_result()
        else:
            op = getattr(self._client, op_name)
            done = False
            data = {}
            while not done:
                try:
                    data = op(**kwargs)
                    done = True
                except ClientError as e:
                    LOG.debug(e, kwargs)
                    if 'Throttling' in str(e):
                        time.sleep(1)
                    elif 'AccessDenied' in str(e):
                        done = True
                    elif 'NoSuchTagSet' in str(e):
                        done = True
                    elif 'Topic does not exist' in str(e):
                        done = True
                except Exception:
                    done = True
        if query:
            data = query.search(data)
        return data


def get_awsclient(service_name, region_name, account_id, **kwargs):
    if region_name == '':
        region_name = None
    return AWSClient(service_name, region_name, account_id, **kwargs)
