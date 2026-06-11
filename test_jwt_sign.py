import os
import sys
import unittest
import time
import base64
from unittest.mock import patch
import jwt
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives import serialization

# Ensure correct path imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

class TestCDPJWTSigning(unittest.TestCase):
    
    @classmethod
    def setUpClass(cls):
        # Generate a mock EC private key for testing
        cls.private_key_ec = ec.generate_private_key(ec.SECP256R1())
        cls.pem_key_ec = cls.private_key_ec.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()
        ).decode("utf-8")
        
        # Generate a mock Ed25519 private key for testing
        cls.private_key_ed = ed25519.Ed25519PrivateKey.generate()
        # To simulate legacy 64-byte key format: seed (32 bytes) + public key (32 bytes)
        seed = cls.private_key_ed.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption()
        )
        pubkey = cls.private_key_ed.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )
        cls.base64_key_ed = base64.b64encode(seed + pubkey).decode("utf-8")
        
        cls.key_name = "organizations/test-org/apiKeys/test-key-id"

    def test_cdp_create_headers_success_ec(self):
        self._run_header_test(
            private_key_str=self.pem_key_ec.replace("\n", "\\n"),
            expected_alg="ES256",
            public_key=self.private_key_ec.public_key()
        )

    def test_cdp_create_headers_success_ed25519(self):
        self._run_header_test(
            private_key_str=self.base64_key_ed,
            expected_alg="EdDSA",
            public_key=self.private_key_ed.public_key()
        )

    def _run_header_test(self, private_key_str, expected_alg, public_key):
        # Mock env vars
        mock_env = {
            "X402_NETWORK": "eip155:8453",
            "CDP_API_KEY_NAME": self.key_name,
            "CDP_API_KEY_PRIVATE_KEY": private_key_str
        }
        
        with patch.dict(os.environ, mock_env):
            # We import main inside the test block to re-execute logic
            if "main" in sys.modules:
                del sys.modules["main"]
            import main
            
            # Retrieve the created header generation function
            self.assertTrue(hasattr(main, "cdp_create_headers"))
            headers = main.cdp_create_headers()
            
            # Verify structure
            self.assertIn("verify", headers)
            self.assertIn("settle", headers)
            self.assertIn("supported", headers)
            
            # Verify specific tokens
            for endpoint_name, endpoint_headers in headers.items():
                if endpoint_name == "bazaar":
                    continue
                self.assertIn("Authorization", endpoint_headers)
                auth_val = endpoint_headers["Authorization"]
                self.assertTrue(auth_val.startswith("Bearer "))
                
                token = auth_val[7:]
                
                # Decode and inspect headers/claims
                unverified_header = jwt.get_unverified_header(token)
                self.assertEqual(unverified_header["alg"], expected_alg)
                self.assertEqual(unverified_header["kid"], self.key_name)
                self.assertEqual(unverified_header.get("typ"), "JWT")
                self.assertIn("nonce", unverified_header)
                
                # Verify claims using the mock public key
                payload = jwt.decode(token, public_key, algorithms=[expected_alg], audience="cdp_service")
                
                self.assertEqual(payload["sub"], self.key_name)
                self.assertEqual(payload["iss"], "cdp")
                self.assertIn("nbf", payload)
                self.assertIn("exp", payload)
                self.assertIn("uris", payload)
                self.assertIsInstance(payload["uris"], list)
                self.assertEqual(len(payload["uris"]), 1)
                
                # Check method/uris mapping
                if endpoint_name == "verify":
                    self.assertEqual(payload["uris"][0], "POST api.cdp.coinbase.com/platform/v2/x402/verify")
                elif endpoint_name == "settle":
                    self.assertEqual(payload["uris"][0], "POST api.cdp.coinbase.com/platform/v2/x402/settle")
                elif endpoint_name == "supported":
                    self.assertEqual(payload["uris"][0], "GET api.cdp.coinbase.com/platform/v2/x402/supported")

    def test_cdp_create_headers_missing_keys(self):
        # Test case where environment variables are empty or missing
        mock_env = {
            "CDP_API_KEY_NAME": "",
            "CDP_API_KEY_ID": "",
            "CDP_API_KEY_PRIVATE_KEY": "",
            "CDP_API_KEY_SECRET": ""
        }
        with patch.dict(os.environ, mock_env):
            if "main" in sys.modules:
                del sys.modules["main"]
            import main
            headers = main.cdp_create_headers()
            self.assertEqual(headers["verify"], {})
            self.assertEqual(headers["settle"], {})
            self.assertEqual(headers["supported"], {})

if __name__ == "__main__":
    unittest.main()
