""" Create and init wallet that stores coldkey and hotkey
"""
# The MIT License (MIT)
# Copyright © 2021 Yuma Rao

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated 
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation 
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, 
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of 
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL 
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION 
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER 
# DEALINGS IN THE SOFTWARE.

import argparse
import copy
import os

import bittensor
from . import wallet_impl
from . import wallet_mock
class wallet:
    """ Create and init wallet that stores hot and coldkey
    """
    @classmethod
    def mock(cls) -> 'bittensor.Wallet':
        return wallet( name='mock' )

    def __new__(
            cls, 
            config: 'bittensor.Config' = None,
            name: str = None,
            hotkey: str = None,
            path: str = None,
            _mock: bool = None
        ) -> 'bittensor.Wallet':
        r""" Init bittensor wallet object containing a hot and coldkey.

            Args:
                config (:obj:`bittensor.Config`, `optional`): 
                    bittensor.wallet.config()
                name (required=False, default='default'):
                    The name of the wallet to unlock for running bittensor
                hotkey (required=False, default='default'):
                    The name of hotkey used to running the miner.
                path (required=False, default='~/.bittensor/wallets/'):
                    The path to your bittensor wallets
                _mock (required=False, default=False):
                    If true creates a mock wallet with random keys.
        """
        if config == None: 
            config = wallet.config()
        config = copy.deepcopy( config )
        config.wallet.name = name if name != None else config.wallet.name
        config.wallet.hotkey = hotkey if hotkey != None else config.wallet.hotkey
        config.wallet.path = path if path != None else config.wallet.path
        config.wallet._mock = _mock if _mock != None else config.wallet._mock
        wallet.check_config( config )
        # Allows mocking from the command line.
        if config.wallet.name == 'mock' or config.wallet._mock:
            config.wallet._mock = True
            _mock = True

            return wallet_mock.Wallet_mock(
                name = config.wallet.name, 
                hotkey = config.wallet.hotkey, 
                path = config.wallet.path,
                _mock = True,
            )

        return wallet_impl.Wallet(
            name = config.wallet.name, 
            hotkey = config.wallet.hotkey, 
            path = config.wallet.path,
        )

    @classmethod   
    def config(cls) -> 'bittensor.Config':
        """ Get config from the argument parser
        Return: bittensor.config object
        """
        parser = argparse.ArgumentParser()
        wallet.add_args( parser )
        return bittensor.config( parser )

    @classmethod   
    def help(cls):
        """ Print help to stdout
        """
        parser = argparse.ArgumentParser()
        cls.add_args( parser )
        print (cls.__new__.__doc__)
        parser.print_help()

    @classmethod
    def add_args(cls, parser: argparse.ArgumentParser ):
        """ Accept specific arguments from parser
        """
        try:
            parser.add_argument('--wallet.name',required=False, default=bittensor.defaults.wallet.name, help='''The name of the wallet to unlock for running bittensor (name mock is reserved for mocking this wallet)''')
            parser.add_argument('--wallet.hotkey', required=False, default=bittensor.defaults.wallet.hotkey, help='''The name of wallet's hotkey.''')
            parser.add_argument('--wallet.path',required=False, default=bittensor.defaults.wallet.path, help='''The path to your bittensor wallets''')
            parser.add_argument('--wallet._mock', action='store_true', default=bittensor.defaults.wallet._mock, help='To turn on wallet mocking for testing purposes.')
        except argparse.ArgumentError:
            # re-parsing arguments.
            pass

    @classmethod   
    def add_defaults(cls, defaults):
        """ Adds parser defaults to object from enviroment variables.
        """
        defaults.wallet = bittensor.Config()
        defaults.wallet.name = os.getenv('BT_WALLET_NAME') if os.getenv('BT_WALLET_NAME') != None else 'default'
        defaults.wallet.hotkey = os.getenv('BT_WALLET_HOTKEY') if os.getenv('BT_WALLET_HOTKEY') != None else 'default'
        defaults.wallet.path = os.getenv('BT_WALLET_PATH') if os.getenv('BT_WALLET_PATH') != None else '~/.bittensor/wallets/'
        defaults.wallet._mock = os.getenv('BT_WALLET_MOCK') if os.getenv('BT_WALLET_MOCK') != None else False

    @classmethod   
    def check_config(cls, config: 'bittensor.Config' ):
        """ Check config for wallet name/hotkey/path
        """
        assert 'wallet' in config
        assert isinstance(config.wallet.name, str)
        assert isinstance(config.wallet.hotkey, str)
        assert isinstance(config.wallet.path, str)
