-- Optional: Unified API also runs CREATE TABLE IF NOT EXISTS on first use.
-- Encrypted (Fernet) integration secrets when POSTGRES_HOST is set on khala.

CREATE TABLE IF NOT EXISTS encrypted_integration_credentials (
    service TEXT NOT NULL,
    credential_key TEXT NOT NULL,
    ciphertext TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (service, credential_key)
);
