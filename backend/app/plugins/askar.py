import json
from fastapi import HTTPException
from aries_askar import Store, error, Key, KeyAlg
from aries_askar.bindings import LocalKeyHandle
from config import settings
import hashlib
import uuid
import httpx
from datetime import datetime, timezone, timedelta
from hashlib import sha256
import secrets
import canonicaljson
from multiformats import multibase
import base64

DEFAULT_ALG = "ed25519"
ALG_MAPPINGS = {"ed25519": {"prefix_hex": "ed01", "prefix_length": 2}}


class AskarWallet:
    def __init__(self):
        self.db = settings.ASKAR_DB
        self.store_key = Store.generate_raw_key(
            hashlib.md5(settings.DOMAIN.encode()).hexdigest()
        )

    def resolve_did_web(self, did):
        r = httpx.get(
            "https://" + did.lstrip("did:web:").replace(":", "/") + "/did.json"
        )
        return r.json()

    def multikey_to_jwk(self, multikey):
        return multikey

    def _to_multikey(self, public_bytes, alg=DEFAULT_ALG):
        prefix_hex = ALG_MAPPINGS[alg]["prefix_hex"]
        return multibase.encode(
            bytes.fromhex(f"{prefix_hex}{public_bytes.hex()}"), "base58btc"
        )

    async def create_key(self, kid=None, seed=None):
        if not seed:
            seed = secrets.token_urlsafe(32)
        store = await Store.open(self.db, "raw", self.store_key)
        key = Key(LocalKeyHandle()).from_seed(KeyAlg.ED25519, seed)
        multikey = self._to_multikey(key.get_public_bytes())
        if not kid:
            kid = f"did:key:{multikey}"
        try:
            async with store.session() as session:
                await session.insert(
                    "key",
                    kid,
                    key.get_secret_bytes(),
                    {"kid": kid, "multikey": multikey},
                )
            return self._to_multikey(key.get_public_bytes())
        except:
            return None

    async def get_key(self, kid):
        store = await Store.open(self.db, "raw", self.store_key)
        async with store.session() as session:
            secret_bytes = await session.fetch("key", kid)
            return Key(LocalKeyHandle()).from_secret_bytes(
                DEFAULT_ALG, secret_bytes.value
            )

    async def get_multikey(self, kid):
        store = await Store.open(self.db, "raw", self.store_key)
        async with store.session() as session:
            secret_bytes = await session.fetch("key", kid)
            return secret_bytes.tags["multikey"]

    async def add_proof(self, document, proof_options):
        existing_proof = document.pop("proof", [])
        assert isinstance(existing_proof, list) or isinstance(existing_proof, dict)
        existing_proof = (
            [existing_proof] if isinstance(existing_proof, dict) else existing_proof
        )

        assert proof_options["type"] == "DataIntegrityProof"
        assert proof_options["cryptosuite"] == "eddsa-jcs-2022"
        assert proof_options["proofPurpose"]
        assert proof_options["verificationMethod"]
        hash_data = (
            sha256(canonicaljson.encode_canonical_json(document)).digest()
            + sha256(canonicaljson.encode_canonical_json(proof_options)).digest()
        )
        kid = proof_options["verificationMethod"].split("#")[0]
        key = await self.get_key(kid)
        proof_bytes = key.sign_message(hash_data)
        proof = proof_options.copy()
        proof["proofValue"] = multibase.encode(proof_bytes, "base58btc")

        secured_document = document.copy()
        secured_document["proof"] = existing_proof
        secured_document["proof"].append(proof)

        return secured_document

    async def sign_vc_jose(self, vc):
        issuer = vc["issuer"]["id"]
        headers = {
            "alg": "EdDSA",
            "kid": f"{issuer}#key-01-jwk",
            "typ": "vc+ld+json",
            "cty": "vc+ld+json",
        }
        encoded_headers = (
            base64.urlsafe_b64encode(json.dumps(headers).encode()).decode().rstrip("=")
        )
        encoded_payload = (
            base64.urlsafe_b64encode(json.dumps(vc).encode()).decode().rstrip("=")
        )
        key = await AskarWallet().get_key(issuer)
        signature = key.sign_message(f"{encoded_headers}.{encoded_payload}".encode())

        encoded_signature = base64.urlsafe_b64encode(signature).decode().rstrip("=")
        jwt_token = f"{encoded_headers}.{encoded_payload}.{encoded_signature}"
        return {
            "@context": "https://www.w3.org/ns/credentials/v2",
            "id": f"data:application/vc+jwt,{jwt_token}",
            "type": "EnvelopedVerifiableCredential",
        }


class AskarStorage:
    def __init__(self):
        self.db = settings.ASKAR_DB
        self.key = Store.generate_raw_key(
            hashlib.md5(settings.DOMAIN.encode()).hexdigest()
        )

    async def provision(self, recreate=False):
        settings.LOGGER.info("Initializaing database")
        settings.LOGGER.info(self.db)
        await Store.provision(self.db, "raw", self.key, recreate=recreate)

    async def open(self):
        return await Store.open(self.db, "raw", self.key)

    async def fetch(self, category, data_key):
        store = await self.open()
        try:
            async with store.session() as session:
                data = await session.fetch(category, data_key)
            return json.loads(data.value)
        except:
            return None

    async def replace(self, category, data_key, data):
        try:
            await self.store(category, data_key, data)
        except:
            try:
                await self.update(category, data_key, data)
            except:
                raise HTTPException(status_code=400, detail="Couldn't replace record.")

    async def store(self, category, data_key, data, tags={}):
        store = await self.open()
        # try:
        async with store.session() as session:
            await session.insert(
                category,
                data_key,
                json.dumps(data),
                tags,
            )
        # except:
        #     raise HTTPException(status_code=400, detail="Couldn't store record.")

    async def update(self, category, data_key, data):
        store = await self.open()
        try:
            async with store.session() as session:
                await session.replace(
                    category,
                    data_key,
                    json.dumps(data),
                    {"~plaintag": "a", "enctag": "b"},
                )
        except:
            raise HTTPException(status_code=404, detail="Couldn't update record.")

    async def add_issuer(self, did, name, description):
        issuer_registrations = await AskarStorage().fetch("registration", "issuers")
        if next(
            (
                issuer
                for issuer in issuer_registrations["issuers"]
                if issuer["id"] == did
            ),
            None,
        ):
            raise HTTPException(status_code=419, detail="Issuer already exists.")
        issuer_registrations["issuers"].append(
            {
                "id": did,
                "name": name,
                "description": description,
            }
        )
        issuer_registrations = await AskarStorage().update(
            "registration", "issuers", issuer_registrations
        )


class AskarVerifier:
    def __init__(self, multikey=None):
        self.type = "DataIntegrityProof"
        self.cryptosuite = "eddsa-jcs-2022"
        self.purpose = "authentication"
        if multikey:
            self.key = Key().from_public_bytes(
                alg="ed25519", public=bytes(bytearray(multibase.decode(multikey))[2:])
            )

    def create_proof_config(self):
        created = str(datetime.now(timezone.utc).isoformat("T", "seconds"))
        expires = str(
            (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(
                "T", "seconds"
            )
        )
        return {
            "type": self.type,
            "cryptosuite": self.cryptosuite,
            "proofPurpose": self.purpose,
            "created": created,
            "expires": expires,
            "domain": settings.DOMAIN,
            "challenge": self.create_challenge(created + expires),
        }

    def create_challenge(self, value):
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, settings.SECRET_KEY + value))

    def assert_proof_options(self, proof):
        try:
            assert proof["type"] == self.type
            assert proof["cryptosuite"] == self.cryptosuite
            assert proof["proofPurpose"] == self.purpose
            # assert datetime.fromisoformat(proof["created"]) < datetime.now(timezone.utc)
            # assert datetime.fromisoformat(proof["expires"]) > datetime.now(timezone.utc)
        except:
            raise HTTPException(status_code=400, detail="Invalid Proof.")

    def verify(self, message, signature):
        return self.verifier.verify_signature(message=message, signature=signature)

    def known_issuer(self, issuer):
        pass

    def verify_proof(self, document, proof):
        self.assert_proof_options(proof)
        assert proof["verificationMethod"].split("#")[0] == document["id"]

        multikey = proof["verificationMethod"].split("#")[-1]
        key = Key(LocalKeyHandle()).from_public_bytes(
            alg="ed25519", public=bytes(bytearray(multibase.decode(multikey))[2:])
        )

        proof_options = proof.copy()
        proof_value = proof_options.pop("proofValue")
        proof_bytes = multibase.decode(proof_value)
        hash_data = (
            sha256(canonicaljson.encode_canonical_json(proof_options)).digest()
            + sha256(canonicaljson.encode_canonical_json(document)).digest()
        )

        try:
            if not key.verify_signature(message=hash_data, signature=proof_bytes):
                raise HTTPException(
                    status_code=400, detail="Signature was forged or corrupt."
                )
        except:
            raise HTTPException(status_code=400, detail="Error verifying proof.")
