"""
CortexAI — Security Module
============================
Gestisce autenticazione e crittografia:
- Hashing password (bcrypt)
- Creazione e validazione JWT token
- Generazione e validazione API key
- Encryption PII (Fernet/AES-256)

FLUSSO DI AUTENTICAZIONE:
┌─────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
│ Client   │───▶│  Nginx   │───▶│  FastAPI  │───▶│  Auth    │
│          │    │          │    │  Middleware│    │  Module  │
└─────────┘    └──────────┘    └──────────┘    └──────────┘
                                                    │
                                            ┌───────┴───────┐
                                            │               │
                                    ┌───────▼──┐    ┌───────▼──┐
                                    │  JWT     │    │  API Key │
                                    │  Verify  │    │  Verify  │
                                    └──────────┘    └──────────┘
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4
from typing import Optional
import hashlib
import secrets

import jwt
from passlib.context import CryptContext
from cryptography.fernet import Fernet

from src.api.config import get_settings


# ---------------------------------------------------------------------------
# PASSWORD HASHING
# ---------------------------------------------------------------------------
# bcrypt è l'algoritmo standard per hashing password.
# È INTENZIONALMENTE lento (~100ms per hash) per rendere il brute force
# impraticabile. Se un attaccante ruba il database, non può recuperare
# le password in tempi ragionevoli.
#
# NOTA: MAI usare MD5 o SHA-256 per password. Sono troppo veloci.
# bcrypt include automaticamente un "salt" random per prevenire
# attacchi con rainbow table.
# ---------------------------------------------------------------------------

pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",  # Aggiorna automaticamente hash vecchi
)


def hash_password(password: str) -> str:
    """
    Genera un hash bcrypt dalla password in chiaro.

    Esempio:
        hash_password("mypassword123")
        → "$2b$12$LJ3m4ys3GZvZ5..."  (60 caratteri, diverso ogni volta!)

    Il risultato è diverso ogni volta perché bcrypt aggiunge un salt random.
    Questo è intenzionale e corretto.
    """
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verifica che una password in chiaro corrisponda al suo hash.

    Esempio:
        verify_password("mypassword123", "$2b$12$LJ3m...")  → True
        verify_password("wrongpassword", "$2b$12$LJ3m...")  → False
    """
    return pwd_context.verify(plain_password, hashed_password)


# ---------------------------------------------------------------------------
# JWT TOKEN
# ---------------------------------------------------------------------------
# JWT (JSON Web Token) è lo standard per autenticazione stateless.
#
# COME FUNZIONA:
# 1. L'utente fa login con email + password
# 2. Il server verifica le credenziali e genera un JWT firmato
# 3. Il client include il JWT in ogni richiesta successiva (header Authorization)
# 4. Il server verifica la firma del JWT SENZA interrogare il database
#    (questo è il vantaggio: nessuna query DB per ogni richiesta)
#
# STRUTTURA JWT:
# header.payload.signature
# - header: algoritmo di firma (HS256)
# - payload: dati utente (user_id, tenant_id, ruolo, scadenza)
# - signature: HMAC-SHA256(header + payload, SECRET_KEY)
#
# SICUREZZA:
# - Il payload è FIRMATO ma NON criptato (chiunque può leggerlo)
# - La firma garantisce che nessuno ha modificato il payload
# - Se qualcuno cambia anche un bit, la verifica fallisce
# ---------------------------------------------------------------------------


def create_access_token(
    user_id: str,
    tenant_id: str,
    role: str,
    permissions: list[str] = None,
) -> str:
    """
    Crea un JWT access token.

    L'access token ha vita breve (default: 60 minuti) e contiene
    tutte le informazioni necessarie per autorizzare una richiesta
    SENZA interrogare il database.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)

    payload = {
        # Standard JWT claims
        "sub": str(user_id),                    # Subject: chi è l'utente
        "iat": now,                             # Issued At: quando è stato creato
        "exp": now + timedelta(minutes=settings.jwt_access_token_expire_minutes),  # Expiration
        "jti": str(uuid4()),                    # JWT ID: identificativo unico (per revoca)

        # Custom claims (specifici di CortexAI)
        "tenant_id": str(tenant_id),
        "role": role,
        "permissions": permissions or [],
        "type": "access",                       # Distingue da refresh token
    }

    token = jwt.encode(
        payload,
        settings.secret_key,
        algorithm=settings.jwt_algorithm,
    )
    return token


def create_refresh_token(user_id: str, tenant_id: str) -> str:
    """
    Crea un JWT refresh token.

    Il refresh token ha vita lunga (default: 7 giorni) e serve SOLO
    per ottenere un nuovo access token quando quello vecchio scade.
    Contiene meno informazioni dell'access token per sicurezza.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)

    payload = {
        "sub": str(user_id),
        "tenant_id": str(tenant_id),
        "iat": now,
        "exp": now + timedelta(days=settings.jwt_refresh_token_expire_days),
        "jti": str(uuid4()),
        "type": "refresh",
    }

    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    """
    Decodifica e verifica un JWT token.

    Se il token è scaduto, modificato, o firmato con una chiave diversa,
    solleva un'eccezione.

    Returns:
        dict: Il payload del token decodificato.

    Raises:
        jwt.ExpiredSignatureError: Token scaduto
        jwt.InvalidTokenError: Token invalido (firma sbagliata, formato errato, etc.)
    """
    settings = get_settings()

    payload = jwt.decode(
        token,
        settings.secret_key,
        algorithms=[settings.jwt_algorithm],
    )
    return payload


# ---------------------------------------------------------------------------
# API KEY
# ---------------------------------------------------------------------------
# Le API key sono per accesso programmatico (agenti AI, integrazioni).
# A differenza dei JWT, non scadono automaticamente (ma possono avere
# una data di scadenza opzionale).
#
# FORMATO: "dnx_" + 48 caratteri random = 52 caratteri totali
# Esempio: "dnx_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v2w3x4"
#
# SICUREZZA:
# - La key completa è mostrata SOLO alla creazione (poi mai più)
# - Nel database salviamo solo lo SHA-256 dell'intera key
# - Per autenticare: SHA-256(key ricevuta) == SHA-256(key nel DB)?
# - Il prefix (primi 8 char) è salvato in chiaro per identificazione nei log
# ---------------------------------------------------------------------------


def generate_api_key() -> tuple[str, str, str]:
    """
    Genera una nuova API key.

    Returns:
        tuple: (key_full, key_prefix, key_hash)
        - key_full: la chiave completa (da mostrare all'utente UNA VOLTA)
        - key_prefix: primi 8 caratteri (per identificazione nei log)
        - key_hash: SHA-256 della chiave (da salvare nel database)
    """
    # Genera 48 bytes casuali crittograficamente sicuri
    random_part = secrets.token_urlsafe(36)  # ~48 caratteri
    key_full = f"dnx_{random_part}"

    key_prefix = key_full[:8]  # "dnx_a1b2"
    key_hash = hashlib.sha256(key_full.encode()).hexdigest()

    return key_full, key_prefix, key_hash


def verify_api_key(key: str, stored_hash: str) -> bool:
    """
    Verifica che una API key corrisponda al suo hash salvato.

    Usa compare_digest per evitare timing attacks
    (attacchi dove si misura il tempo di confronto per indovinare la key).
    """
    computed_hash = hashlib.sha256(key.encode()).hexdigest()
    return secrets.compare_digest(computed_hash, stored_hash)


# ---------------------------------------------------------------------------
# PII ENCRYPTION (Fernet / AES-256)
# ---------------------------------------------------------------------------
# Fernet è una implementazione di AES-256 in modalità CBC con HMAC-SHA256.
# Usata per criptare i campi PII (Personally Identifiable Information)
# nel database (es. email in certi contesti, numeri di telefono, etc.).
#
# PERCHÉ CRIPTARE I PII:
# - GDPR richiede protezione dei dati personali "at rest" (nel database)
# - Se un attaccante accede al database, i PII sono illeggibili
# - Per leggere i dati serve la chiave di encryption (nel .env, non nel DB)
# ---------------------------------------------------------------------------


def get_fernet() -> Fernet:
    """
    Restituisce l'istanza Fernet per encryption/decryption.
    La chiave è derivata dal SECRET_KEY dell'applicazione.
    """
    settings = get_settings()
    # Deriva una chiave Fernet valida dal secret_key (32 bytes, base64 encoded)
    import base64
    key_bytes = settings.secret_key.encode()[:32].ljust(32, b'\0')
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    return Fernet(fernet_key)


def encrypt_pii(value: str) -> str:
    """Cripta un valore PII. Restituisce il testo cifrato come stringa."""
    f = get_fernet()
    return f.encrypt(value.encode()).decode()


def decrypt_pii(encrypted_value: str) -> str:
    """Decripta un valore PII. Restituisce il testo in chiaro."""
    f = get_fernet()
    return f.decrypt(encrypted_value.encode()).decode()
