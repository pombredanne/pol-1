""" Implementation of pol safes.  See `Safe`. """

import time
import struct
import logging
import binascii
import multiprocessing

import pol.blockcipher
import pol.parallel
import pol.elgamal
import pol.ks
import pol.kd

import msgpack
import gmpy

# TODO Generating random numbers seems CPU-bound.  Does the default random
#      generator wait for a certain amount of entropy?
import Crypto.Random
import Crypto.Random.random as random

l = logging.getLogger(__name__)

# Constants used for access slices
AS_MAGIC = binascii.unhexlify('1a1a8ad7')  # starting bytes of an access slice
AS_FULL = 0         # the access slice gives full access
AS_LIST = 1         # the access slice gives list-only access
AS_APPEND = 2       # the access slice gives append-only access

# We derive multiple keys from one base key using hashing and
# constants. For instance, given a base key K, the ElGamal private
# key for of the n-th block is KeyDerivation(K, KD_ELGAMAL, n)
KD_ELGAMAL = binascii.unhexlify('d53d376a7db498956d7d7f5e570509d5')
KD_SYMM    = binascii.unhexlify('4110252b740b03c53b1c11d6373743fb')
KD_LIST    = binascii.unhexlify('d53d376a7db498956d7d7f5e570509d5')
KD_APPEND  = binascii.unhexlify('76001c344cbd9e73a6b5bd48b67266d9')

class WrongKeyError(ValueError):
    pass

class SafeFullError(ValueError):
    pass

class SafeFormatError(ValueError):
    pass

class Safe(object):
    """ A pol safe deniably stores containers. (Containers store secrets.) """

    def __init__(self, data):
        self.data = data
        if 'key-stretching' not in self.data:
            raise SafeFormatError("Missing `key-stretching' attribute")
        if 'key-derivation' not in self.data:
            raise SafeFormatError("Missing `key-derivation' attribute")
        if 'block-cipher' not in self.data:
            raise SafeFormatError("Missing `block-cipher' attribute")
        self.ks = pol.ks.KeyStretching.setup(self.data['key-stretching'])
        self.kd = pol.kd.KeyDerivation.setup(self.data['key-derivation'])
        self.cipher = pol.blockcipher.BlockCipher.setup(
                            self.data['block-cipher'])

    def store(self, stream):
        start_time = time.time()
        l.info('Packing ...')
        msgpack.pack(self.data, stream)
        l.info(' packed in %.2fs', time.time() - start_time)

    @staticmethod
    def load(stream):
        start_time = time.time()
        l.info('Unpacking ...')
        data = msgpack.unpack(stream, use_list=True)
        l.info(' unpacked in %.2fs', time.time() - start_time)
        if ('type' not in data or not isinstance(data['type'], basestring)
                or data['type'] not in TYPE_MAP):
            raise SafeFormatError("Invalid `type' attribute")
        return TYPE_MAP[data['type']](data)

    @staticmethod
    def generate(typ='elgamal', *args, **kwargs):
        if typ not in TYPE_MAP:
            raise ValueError("I do not know Safe type %s" % typ)
        return TYPE_MAP[typ].generate(*args, **kwargs)

    def new_container(self, password, list_password=None, append_password=None):
        """ Create a new container. """
        raise NotImplementedError

    def open_container(self, password):
        """ Opens a container. """
        raise NotImplementedError

    def rerandomize(self):
        """ Rerandomizes the safe. """
        raise NotImplementedError

class ElGamalSafe(Safe):
    """ Default implementation using rerandomization of ElGamal. """

    class Slice(object):
        def __init__(self, safe, first_index, indices):
            self.safe = safe
            self.indices = indices
            self.first_index = first_index
        def trash(self, randfunc=None):
            """ Destroy contents of this slice by writing random values. """
            if randfunc is None:
                randfunc = Crypto.Random.new().read
            # Generate a key, annex the blocks and store random data.
            key = randfunc(self.safe.kd.size)
            pt = randfunc(self.size)
            self.store(key, pt, randfunc, annex=True)
        @property
        def size(self):
            """ The amount of plaintext bytes this slice can store. """
            return (len(self.indices) * (self.safe.bytes_per_block
                                            - self.safe.block_index_size)
                        - self.safe.cipher.blocksize - self.safe.slice_size)

        def store(self, key, value, randfunc=None, annex=False):
            """ Stores `value' in the slice """
            if randfunc is None:
                randfunc = Crypto.Random.new().read
            bpb = self.safe.bytes_per_block
            # First, get the full length plaintext string
            total_size = self.size
            if len(value) > total_size:
                raise ValueError("`value' too large")
            raw = self.safe._slice_size_to_bytes(len(value)) + value
            raw = raw.ljust(total_size, '\0')
            # Secondly, generate an IV, shuffle indices and get a cipherstream
            iv = randfunc(self.safe.cipher.blocksize)
            other_indices = list(self.indices)
            random.shuffle(other_indices)
            cipher = self.safe._cipherstream(key, iv)
            # Thirdly, write the first block
            first_block_pt_size = (bpb - self.safe.cipher.blocksize
                                        - self.safe.block_index_size)
            if other_indices:
                second_block = other_indices[0]
            else:
                second_block = first_block
            first_block_ct = iv + cipher.encrypt(raw[:first_block_pt_size]
                                + self.safe._index_to_bytes(second_block))
            self.safe._eg_encrypt_block(key, self.first_index, first_block_ct,
                                            randfunc, annex=annex)
            offset = first_block_pt_size
            ptsize = bpb - self.safe.block_index_size
            # Finally, write the remaining blocks
            for indexindex, index in enumerate(other_indices):
                if indexindex + 1 < len(other_indices):
                    next_index = other_indices[indexindex + 1]
                else:
                    next_index = index
                ct = cipher.encrypt(raw[offset:offset+ptsize] +
                                self.safe._index_to_bytes(next_index))
                self.safe._eg_encrypt_block(key, index, ct, randfunc,
                                                annex=annex)

    def __init__(self, data):
        super(ElGamalSafe, self).__init__(data)
        # Check if `data' makes sense.
        self.free_blocks = set([])
        for attr in ('group-params', 'n-blocks', 'blocks', 'block-index-size',
                            'slice-size'):
            if not attr in data:
                raise SafeFormatError("Missing attr `%s'" % attr)
        for attr, _type in {'blocks': list,
                            'group-params': list,
                            'block-index-size': int,
                            'slice-size': int,
                            'bytes-per-block': int,
                            'n-blocks': int}.iteritems():
            if not isinstance(data[attr], _type):
                raise SafeFormatError("`%s' should be a `%s'" % (attr, _type))
        if not len(data['blocks']) == data['n-blocks']:
            raise SafeFormatError("Amount of blocks isn't `n-blocks'")
        if not len(data['group-params']) == 2:
            raise SafeFormatError("`group-params' should contain 2 elements")
        # TODO Should we check whether the group parameters are safe?
        for x in data['group-params']:
            if not isinstance(x, basestring):
                raise SafeFormatError("`group-params' should contain strings")
        if data['slice-size'] == 2:
            self._slice_size_struct = struct.Struct('>H')
        elif data['slice-size'] == 4:
            self._slice_size_struct = struct.Struct('>I')
        else:
            raise SafeFormatError("`slice-size' invalid")
        if data['block-index-size'] == 1:
            self._block_index_struct = struct.Struct('>B')
        elif data['block-index-size'] == 2:
            self._block_index_struct = struct.Struct('>H')
        elif data['block-index-size'] == 4:
            self._block_index_struct = struct.Struct('>I')
        else:
            raise SafeFormatError("`block-index-size' invalid")
        if 2** (data['bytes-per-block']*8) >= self.group_params.p:
            raise SafeFormatError("`bytes-per-block' larger than "+
                                  "`group-params' allow")
    @staticmethod
    def generate(n_blocks=1024, block_index_size=2, slice_size=4,
                    ks=None, kd=None, blockcipher=None, gp_bits=1025,
                    precomputed_gp=False, nworkers=None, use_threads=False,
                    progress=None):
        """ Creates a new safe. """
        # TODO check whether block_index_size, slice_size, gp_bits and
        #      n_blocks are sane.
        # First, set the defaults
        if precomputed_gp:
            gp = pol.elgamal.precomputed_group_params(gp_bits)
        else:
            gp = pol.elgamal.generate_group_params(bits=gp_bits,
                    nworkers=nworkers, progress=progress,
                    use_threads=use_threads)
        if ks is None:
            ks = pol.ks.KeyStretching.setup()
        if kd is None:
            kd = pol.kd.KeyDerivation.setup()
        if blockcipher is None:
            cipher = pol.blockcipher.BlockCipher.setup()
        # Now, calculate the useful bytes per block
        bytes_per_block = (gp_bits - 1) / 8
        bytes_per_block = bytes_per_block - bytes_per_block % cipher.blocksize
        # Initialize the safe object
        safe = ElGamalSafe(
                {'type': 'elgamal',
                 'n-blocks': n_blocks,
                 'bytes-per-block': bytes_per_block,
                 'block-index-size': block_index_size,
                 'slice-size': slice_size,
                 'group-params': [x.binary() for x in gp],
                 'key-stretching': ks.params,
                 'key-derivation': kd.params,
                 'block-cipher': cipher.params,
                 'blocks': [['','',''] for i in xrange(n_blocks)]})
        # Mark all blocks as free
        safe.mark_free(xrange(n_blocks))
        return safe

    @property
    def nblocks(self):
        """ Number of blocks. """
        return self.data['n-blocks']

    @property
    def bytes_per_block(self):
        """ Number of bytes stored per block. """
        return self.data['bytes-per-block']

    @property
    def block_index_size(self):
        """ Size of a block index. """
        return self.data['block-index-size']

    @property
    def slice_size(self):
        """ The size of the sizefield of a slice.
            Thus actually: slice_size_size """
        return self.data['slice-size']

    @property
    def group_params(self):
        """ The group parameters. """
        return pol.elgamal.group_parameters(
                    *[gmpy.mpz(x, 256) for x in self.data['group-params']])

    def mark_free(self, indices):
        """ Marks the given indices as free. """
        self.free_blocks.update(indices)

    def rerandomize(self, nworkers=None, use_threads=False, progress=None):
        """ Rerandomizes blocks: they will still decrypt to the same
            plaintext. """
        _progress = None
        if progress is not None:
            def _progress(n):
                progress(float(n) / self.nblocks)
        if not nworkers:
            nworkers = multiprocessing.cpu_count()
        l.debug("Rerandomizing %s blocks on %s workers ...",
                    self.nblocks, nworkers)
        start_time = time.time()
        gp = self.group_params
        self.data['blocks'] = pol.parallel.parallel_map(_eg_rerandomize_block,
                        self.data['blocks'], args=(gp.g, gp.p),
                        nworkers=nworkers, use_threads=use_threads,
                        initializer=_eg_rerandomize_block_initializer,
                        chunk_size=16, progress=_progress)
        secs = time.time() - start_time
        kbps = self.nblocks * gmpy.numdigits(gp.p,2) / 1024.0 / 8.0 / secs
        if progress is not None:
            progress(1.0)
        l.debug(" done in %.2fs; that is %.2f KB/s", secs, kbps)

    def _new_slice(self, nblocks):
        """ Allocates a new slice with `nblocks' space. """
        if len(self.free_blocks) < nblocks:
            raise SafeFullError
        if nblocks == 0:
            raise ValueError("`nblocks' should be positive")
        free_blocks = list(self.free_blocks)
        random.shuffle(free_blocks)
        indices = free_blocks[:nblocks]
        self.free_blocks = set(free_blocks[nblocks:])
        ret = ElGamalSafe.Slice(self, random.choice(indices), indices)
        return ret

    def _index_to_bytes(self, index):
        return self._block_index_struct.pack(index)
    def _index_from_bytes(self, s):
        self._block_index_struct.unpack(s)[0]
    def _slice_size_to_bytes(self, size):
        return self._slice_size_struct.pack(size)
    def _slice_size_from_bytes(self, s):
        self._slice_size_struct.unpack(s)[0]

    def _cipherstream(self, key, iv):
        """ Returns a blockcipher stream for key `key' """
        return self.cipher.new_stream(
                self.kd([key, KD_SYMM], length=self.cipher.keysize), iv)
    def _privkey_for_block(self, key, index):
        """ Returns the elgamal private key for the block `index' """
        # TODO we should not assume how mpz.binary() works
        # TODO is it safe to reduce the size of privkey by this much?
        return gmpy.mpz(self.kd([key, KD_ELGAMAL, self._index_to_bytes(index)],
                            length=self.bytes_per_block) + '\0', 256)
    def _check_key_for_block(self, key, index):
        """ Checks whether `key' decrypts the block `index' """
        pubkey = gmpy.mpz(self.data['blocks'][index][2])
        privkey = self._privkey_for_block(key, index)
        gp = self.group_params
    def _annex_block(self, key, index):
        """ Changes the public key of the block `index' to the one derived from
            base key `key'. """
        self.data['blocks'][index][2] = pol.elgamal.pubkey_from_privkey(
                self._privkey_for_block(key, index), self.group_params).binary()

    # ElGamal encryption and decryption
    def _eg_decrypt_block(self, key, index):
        """ Decrypts the block `index' with `key' """
        privkey = self._privkey_for_block(key, index)
        gp = self.group_params
        pubkey = pol.elgamal.pubkey_from_privkey(privkey, gp)
        if self.data['blocks'][index][2] != pubkey.binary():
            raise WrongKeyError
        c1 = gmpy.mpz(self.data['blocks'][index][0], 256)
        c2 = gmpy.mpz(self.data['blocks'][index][1], 256)
        return pol.elgamal.decrypt(c1, c2, privkey, gp, self.bytes_per_block)
    def _eg_encrypt_block(self, key, index, s, randfunc, annex=False):
        """ Sets the El-Gamal encrypted content of block `index' to `s'
            using key `key' """
        # TODO is there a cheaper way to check whether this block belongs
        #      to this key?
        privkey = self._privkey_for_block(key, index)
        gp = self.group_params
        pubkey = pol.elgamal.pubkey_from_privkey(privkey, gp)
        binary_pubkey = pubkey.binary()
        if self.data['blocks'][index][2] != binary_pubkey:
            if not annex:
                raise WrongKeyError
            self.data['blocks'][index][2] = binary_pubkey
        # TODO is it safe to pick r so much smaller than p?
        c1, c2 = pol.elgamal.encrypt(s, pubkey, gp,
                                     self.bytes_per_block, randfunc)
        self.data['blocks'][index][0] = c1.binary()
        self.data['blocks'][index][1] = c2.binary()

def _eg_rerandomize_block_initializer(args, kwargs):
    Crypto.Random.atfork()
def _eg_rerandomize_block(raw_b, g, p):
    """ Rerandomizes raw_b given group parameters g and p. """
    s = random.randint(2, int(p))
    b = [gmpy.mpz(raw_b[0], 256),
         gmpy.mpz(raw_b[1], 256),
         gmpy.mpz(raw_b[2], 256)]
    b[0] = (b[0] * pow(g, s, p)) % p
    b[1] = (b[1] * pow(b[2], s, p)) % p
    raw_b[0] = b[0].binary()
    raw_b[1] = b[1].binary()
    return raw_b

TYPE_MAP = {'elgamal': ElGamalSafe}
