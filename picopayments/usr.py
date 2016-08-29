# coding: utf-8
# Copyright (c) 2016 Fabian Barkhau <f483@storj.io>
# License: MIT (see LICENSE file)


import os
from picopayments import RPC
from counterpartylib.lib.micropayments import util
from counterpartylib.lib.micropayments.scripts import sign_deposit
from counterpartylib.lib.micropayments.scripts import sign_created_commit


class Client(object):

    _SERIALIZABLE_ATTRS = [
        "handle",  # set once
        "channel_terms",  # set once
        "client_wif",  # set once
        "client_pubkey",  # set once
        "hub_pubkey",  # set once
        "secrets",  # append only
        "c2h_state",  # mutable
        "c2h_spend_secret_hash",  # set once
        "c2h_commit_delay_time",  # set once
        "c2h_next_revoke_secret_hash",  # mutable
        "c2h_deposit_expire_time",  # set once
        "c2h_deposit_quantity",  # set once
        "h2c_state",  # set once
        "payments_sent",
        "payments_received",
        "payments_queued",
    ]

    def __init__(self, url, auth_wif=None, username=None,
                 password=None, verify_ssl_cert=True):
        self.rpc = RPC(url, auth_wif=auth_wif, username=username,
                       password=password, verify_ssl_cert=False)
        for attr in self._SERIALIZABLE_ATTRS:
            setattr(self, attr, None)

    @classmethod
    def deserialize(cls, data):
        """TODO doc string"""
        obj = cls(**data["hub"])
        for attr in obj._SERIALIZABLE_ATTRS:
            setattr(obj, attr, data[attr])
        return obj

    def serialize(self):
        """TODO doc string"""
        data = {
            "hub": {
                "url": self.rpc.url,
                "auth_wif": self.rpc.auth_wif,
                "username": self.rpc.username,
                "password": self.rpc.password,
                "verify_ssl_cert": self.rpc.verify_ssl_cert,
            }
        }
        for attr in self._SERIALIZABLE_ATTRS:
            data[attr] = getattr(self, attr)
        return data

    def get_tx(self, txid):
        """TODO doc string"""
        return self.rpc.getrawtransaction(tx_hash=txid)

    def block_send(self, src_wif, dest_address, asset, quantity):
        """TODO doc string"""

        # FIXME add fee and dust size args
        src_address = util.wif2address(src_wif)
        unsigned_rawtx = self.rpc.create_send(
            source=src_address, destination=dest_address,
            quantity=quantity, asset=asset, regular_dust_size=200000,
        )
        signed_rawtx = sign_deposit(self.get_tx, src_wif, unsigned_rawtx)
        return self.rpc.sendrawtransaction(tx_hex=signed_rawtx)

    def micro_send(self, handle, quantity, token=None):
        """TODO doc string"""

        assert(self.connected())
        if token is None:
            token = util.b2h(os.urandom(32))
        self.payments_queued.append({
            "payee_handle": handle,
            "amount": quantity,
            "token": token
        })
        return token

    def sync(self):
        """TODO doc string"""

        assert(self.connected())
        pass  # FIXME implement

    def connected(self):
        """Returns True if connected to a hub"""
        return bool(self.handle)

    def create_commit(self, quantity):
        """TODO doc string"""
        result = self.rpc.mpc_create_commit(
            state=self.c2h_state,
            quantity=quantity,
            revoke_secret_hash=self.c2h_next_revoke_secret_hash,
            delay_time=self.c2h_commit_delay_time
        )
        script = result["commit_script"]
        rawtx = sign_created_commit(
            self.get_tx,
            self.client_wif,
            result["tosign"]["commit_rawtx"],
            result["tosign"]["deposit_script"],
        )
        return {"rawtx": rawtx, "script": script}

    def connect(self, quantity, expire_time, asset="XCP",
                delay_time=2, own_url=None):
        """TODO doc string"""

        assert(not self.connected())
        self.asset = asset
        self.client_wif = self.rpc.auth_wif
        self.own_url = own_url
        self.client_pubkey = util.wif2pubkey(self.client_wif)
        self.c2h_deposit_expire_time = expire_time
        self.c2h_deposit_quantity = quantity
        h2c_next_revoke_secret_hash = self._create_initial_secrets()
        self._request_connection()
        self._validate_matches_terms()
        c2h_deposit_rawtx = self._make_deposit()
        h2c_deposit_script = self._exchange_deposit_scripts(
            h2c_next_revoke_secret_hash
        )
        c2h_deposit_txid = self._sign_and_publish_deposit(c2h_deposit_rawtx)
        self._set_initial_h2c_state(h2c_deposit_script)
        self.payments_sent = []
        self.payments_received = []
        self.payments_queued = []
        self.c2h_commit_delay_time = delay_time
        return c2h_deposit_txid

    def _create_initial_secrets(self):
        h2c_spend_secret_value = util.b2h(os.urandom(32))
        self.h2c_spend_secret_hash = util.hash160hex(h2c_spend_secret_value)
        h2c_next_revoke_secret_value = util.b2h(os.urandom(32))
        h2c_next_revoke_secret_hash = util.hash160hex(
            h2c_next_revoke_secret_value
        )
        self.secrets = {
            self.h2c_spend_secret_hash: h2c_spend_secret_value,
            h2c_next_revoke_secret_hash: h2c_next_revoke_secret_value
        }
        return h2c_next_revoke_secret_hash

    def _request_connection(self):
        result = self.rpc.mpc_hub_request(
            asset=self.asset, url=self.own_url,
            spend_secret_hash=self.h2c_spend_secret_hash
        )
        self.handle = result["handle"]
        self.channel_terms = result["channel_terms"]
        self.hub_pubkey = result["pubkey"]
        self.c2h_spend_secret_hash = result["spend_secret_hash"]

    def _exchange_deposit_scripts(self, h2c_next_revoke_secret_hash):
        result = self.rpc.mpc_hub_deposit(
            handle=self.handle,
            asset=self.asset,
            deposit_script=self.c2h_state["deposit_script"],
            next_revoke_secret_hash=h2c_next_revoke_secret_hash
        )
        h2c_deposit_script = result["deposit_script"]
        self.c2h_next_revoke_secret_hash = result["next_revoke_secret_hash"]
        return h2c_deposit_script

    def _make_deposit(self):
        result = self.rpc.mpc_make_deposit(
            asset=self.asset,
            payer_pubkey=self.client_pubkey,
            payee_pubkey=self.hub_pubkey,
            spend_secret_hash=self.c2h_spend_secret_hash,
            expire_time=self.c2h_deposit_expire_time,
            quantity=self.c2h_deposit_quantity
        )
        self.c2h_state = result["state"]
        return result["topublish"]

    def _sign_and_publish_deposit(self, c2h_deposit_rawtx):
        signed_c2h_deposit_rawtx = sign_deposit(
            self.get_tx, self.client_wif, c2h_deposit_rawtx
        )
        return self.rpc.sendrawtransaction(tx_hex=signed_c2h_deposit_rawtx)

    def _validate_matches_terms(self):
        timeout_limit = self.channel_terms["timeout_limit"]
        if timeout_limit != 0:
            assert(self.c2h_deposit_expire_time <= timeout_limit)
        deposit_limit = self.channel_terms["deposit_limit"]
        if deposit_limit != 0:
            assert(self.c2h_deposit_quantity <= deposit_limit)

    def _set_initial_h2c_state(self, h2c_deposit_script):
        self.h2c_state = {
            "asset": self.asset,
            "deposit_script": h2c_deposit_script,
            "commits_requested": [],
            "commits_active": [],
            "commits_revoked": [],
        }