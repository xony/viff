# Copyright 2010 VIFF Development Team.
#
# This file is part of VIFF, the Virtual Ideal Functionality Framework.
#
# VIFF is free software: you can redistribute it and/or modify it
# under the terms of the GNU Lesser General Public License (LGPL) as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# VIFF is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE. See the GNU Lesser General
# Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with VIFF. If not, see <http://www.gnu.org/licenses/>.

"""Triple generation for the BeDOZa protocol.
    TODO: Explain more.
"""

from twisted.internet.defer import Deferred, gatherResults, succeed

from viff.runtime import Runtime, Share, ShareList, gather_shares
from viff.field import FieldElement, GF
from viff.constants import TEXT
from viff.util import rand

from bedoza import BeDOZaKeyList, BeDOZaMessageList, BeDOZaShare

# TODO: Use secure random instead!
from random import Random

from hash_broadcast import HashBroadcastMixin

try:
    import pypaillier
except ImportError:
    # The pypaillier module is not released yet, so we cannot expect
    # the import to work.
    print "Error: The pypaillier module or one of the used functions " \
        "are not available."

class Triple(object):
    def __init__(self, a, b, c):
        self.a, self.b, self.c = a, b, c
    def __str__(self):
        return "(%s,%s,%s)" % (self.a, self.b, self.c)


def _send(runtime, vals, serialize=str, deserialize=int):
    """Send vals[i] to player i + 1. Returns deferred list.

    Works as default for integers. If other stuff has to be
    sent, supply another serialization, deserialition.
    """
    pc = tuple(runtime.program_counter)
    for p in runtime.players:
        msg = serialize(vals[p - 1])
        runtime.protocols[p].sendData(pc, TEXT, msg)
    def err_handler(err):
        print err
    values = []
    for p in runtime.players:
        d = Deferred()
        d.addCallbacks(deserialize, err_handler)
        runtime._expect_data(p, TEXT, d)
        values.append(d)
    result = gatherResults(values)
    return result

def _convolute(runtime, val, serialize=str, deserialize=int):
    """As send, but sends the same val to all players."""
    return _send(runtime, [val] * runtime.num_players,
                 serialize=serialize, deserialize=deserialize)

def _convolute_gf_elm(runtime, gf_elm):
    return _convolute(runtime, gf_elm,
                      serialize=lambda x: str(x.value),
                      deserialize=lambda x: gf_elm.field(int(x)))

def _send_gf_elm(runtime, vals):
    return _send(runtime, vals, 
                 serialize=lambda x: str(x.value),
                 deserialize=lambda x: gf_elm.field(int(x)))




class PartialShareContents(object):
    """A BeDOZa share without macs, e.g. < a >.
    TODO: BeDOZaShare should extend this class?
    
    TODO: Should the partial share contain the public encrypted shares?
    TODO: It may be wrong to pass encrypted_shares to super constructor; 
      does it mean that the already public values get passed along on the
      network even though all players already posess them?
    """
    def __init__(self, value, enc_shares):
        self.value = value
        self.enc_shares = enc_shares

    def __str__(self):
        return "PartialShareContents(%d; %s)" % (self.value, self.enc_shares)

# In VIFF, callbacks get the *contents* of a share as input. Hence, in order
# to get a PartialShare as input to callbacks, we need this extra level of
# wrapper indirection.
class PartialShare(Share):
    def __init__(self, runtime, value, enc_shares):
        partial_share_contents = PartialShareContents(value, enc_shares)
        Share.__init__(self, runtime, value.field, partial_share_contents)



class ModifiedPaillier(object):
    """See Ivan's paper, beginning of section 6."""

    def __init__(self, runtime, random):
        self.runtime = runtime;
        self.random = random

    def encrypt(self, value, player_id=None):
        """Encrypt using public key of player player_id. Defaults to own public key."""
        assert isinstance(value, int), \
            "paillier encrypts only integers, got %s" % value.__class__        
        # TODO: Assert value in the right range.
        
        if not player_id:
            player_id = self.runtime.id

        pubkey = self.runtime.players[player_id].pubkey

        randomness = self.random.randint(1, long(pubkey['n']))
        # TODO: Transform value.
        enc = pypaillier.encrypt_r(value, randomness, pubkey)
        return enc

    def decrypt(self, enc_value):
        """Decrypt using own private key."""
        assert isinstance(enc_value, long), \
            "paillier decrypts only longs, got %s" % enc_value.__class__
        # TODO: Assert enc_value in the right range.
        seckey = self.runtime.players[self.runtime.id].seckey
        return pypaillier.decrypt(enc_value, seckey)


class TripleGenerator(object):

    def __init__(self, runtime, p, random):
        assert p > 1
        self.random = random
        # TODO: Generate Paillier cipher with N_i sufficiently larger than p
        self.runtime = runtime
        self.p = p
        self.Zp = GF(p)
        self.k = self._bit_length_of(p)

        paillier_random = Random(self.random.getrandbits(128))
        alpha_random = Random(self.random.getrandbits(128))
        self.paillier = ModifiedPaillier(runtime, paillier_random)
        
        # Debug output.
        #print "n_%d**2:%d" % (runtime.id, self.paillier.pubkey['n_square'])
        #print "n_%d:%d" % (runtime.id, self.paillier.pubkey['n'])
        #print "n_%d bitlength: %d" % (runtime.id, self._bit_length_of(self.paillier.pubkey['n']))

        #self.Zp = GF(p)
        #self.Zn2 = GF(self.paillier.pubkey['n_square'])
        #self.alpha = self.Zp(self.random.randint(0, p - 1))
        self.alpha = alpha_random.randint(0, p - 1)
        self.n2 = runtime.players[runtime.id].pubkey['n_square']

    def _bit_length_of(self, i):
        bit_length = 0
        while i:
            i >>= 1
            bit_length += 1
        return bit_length

    def generate_triples(self, n):
        """Generates and returns a set of n triples.
        
        Data sent over the network is packaged in large hunks in order
        to optimize. TODO: Explain better.

        TODO: This method needs to have enough RAM to represent all n
        triples in memory at the same time. Is there a nice way to
        stream this, e.g. by using Python generators?
        """
        triples = self._generate_passive_triples(n)
        # TODO: Do some ZK stuff.

    def _generate_passive_triples(self, n):
        """Generates and returns a set of n passive tuples.
        
        E.g. where consistency is only guaranteed if all players follow the
        protool.
        """
        pass
    
    def _add_macs(self, partial_shares):
        """Adds macs to the set of PartialBeDOZaShares.
        
        Returns a list of full shares, e.g. including macs.
        (the full shares are deferreds of type BeDOZaShare.)
        """
        
        # TODO: Currently only does this for one partial share.

        # TODO: Would be nice with a class ShareContents like the class
        # PartialShareContents used here.
        
        self.runtime.increment_pc() # Huh!?

        mac_keys = []

        i = 0

        c_list = []
        for j in range(self.runtime.num_players):
            # TODO: This is probably not the fastes way to generate
            # the betas.
            beta = self.random.randint(0, 2**(4 * self.k))
        
            # TODO: Outcommented until mod paillier works for negative numbers.
            #if rand.choice([True, False]):
            #    beta = -beta
            
            enc_beta = self.paillier.encrypt(beta, player_id=j+1)
            c_j = partial_shares[i].enc_shares[j]
            c = (pow(c_j, self.alpha, self.n2) * enc_beta) % self.n2
            if self.runtime.id == 1 and j + 1 == 2:
                print
                print
                print "p=", self.p
                print "player 1: public key of player %s is %s" % (j+1, 
                                                                   self.runtime.players[j+1].pubkey)
                print "player %s encrypted share: %s" % (j+1, partial_shares[i].enc_shares[j])
                print "alpha = %s = %s" % (self.alpha, self.Zp(self.alpha))
                print "beta = %s = %s" %  (beta, self.Zp(beta))
                print "enc_beta =", enc_beta
                print "Player 1 sends c = %s to player %d" % (c, j+1)
                print
                print
            c_list.append(c)
            mac_keys.append(self.Zp(beta))
        received_cs = _send(self.runtime, c_list)

        def finish_sharing(recevied_cs):
            mac_key_list = BeDOZaKeyList(self.alpha, mac_keys)
            # print "received cs:", received_cs.result
            decrypted_cs = [self.Zp(self.paillier.decrypt(c)) for c in received_cs.result]
            if self.runtime.id == 2:
                print
                print
                print "PLAYER2: Got %s from player 1" % received_cs.result[0]
                print "PLAYER2: Decrypted c from player 1: %s" % decrypted_cs[0]
                print "PLAYER2: My share a_2 =", partial_shares[0].value
                print

            mac_msg_list = BeDOZaMessageList(decrypted_cs)
            # Twisted HACK: Need to pack share into tuple.
            return BeDOZaShare(self.runtime,
                               partial_shares[i].value.field,
                               partial_shares[i].value,
                               mac_key_list,
                               mac_msg_list),

        self.runtime.schedule_callback(received_cs, finish_sharing)
        return [received_cs]

        # for player i:
        #     receive c from player i and set 
        #         m^i=Decrypt(c)
    
    def _mul(self):
        pass
    
    def _full_mul(self):
        pass


# TODO: Represent all numbers by GF objects, Zp, Zn, etc.
# E.g. paillier encrypt should return Zn^2 elms and decrypt should
# return Zp elements.