import os
import time
import json
import hashlib
import hmac
import base64
import secrets
import random
import re
from datetime import datetime, timedelta
from functools import wraps
from flask import request, jsonify, abort, current_app, session
from typing import Dict, Any, List, Callable, Optional, Tuple, Union
from urllib.parse import urlparse

# Sistema de tokens para autorização de API
JWT_SECRET = os.environ.get("JWT_SECRET") or secrets.token_hex(32)
TOKEN_EXPIRY_MINUTES = 10  # Token válido por 10 minutos

# Configuração do rate limiter
API_RATE_LIMITS = {
    "create_pix_payment": {"count": 3, "per_minutes": 5},     # 3 solicitações por 5 minutos
    "check_payment_status": {"count": 20, "per_minutes": 5},  # 20 verificações por 5 minutos
    "verify_cpf": {"count": 3, "per_minutes": 5},             # 3 verificações de CPF por 5 minutos
    "default": {"count": 20, "per_minutes": 15}               # Limite padrão
}

# Armazenamento de rate limiting
# Estrutura: {"ip+rota": {"count": N, "reset_at": timestamp}}
RATE_LIMIT_STORE = {}

# Armazenamento de nonces para prevenção de replays
# Estrutura: {"nonce": expiry_timestamp}
NONCE_STORE = {}

# Domínios permitidos para referer
ALLOWED_REFERERS = [
    "encceja2025.com.br",
    "www.encceja2025.com.br",
    "localhost",
    "127.0.0.1"
]

# Tokens anti-CSRF
CSRF_TOKENS = {}

def generate_csrf_token() -> str:
    """Gera um token anti-CSRF único"""
    token = secrets.token_hex(16)
    CSRF_TOKENS[token] = datetime.now() + timedelta(hours=2)  # Válido por 2 horas
    return token

def clean_expired_csrf_tokens() -> None:
    """Remove tokens CSRF expirados"""
    now = datetime.now()
    expired = [token for token, expiry in CSRF_TOKENS.items() if now > expiry]
    for token in expired:
        CSRF_TOKENS.pop(token, None)

def create_jwt_token(data: Dict[str, Any]) -> str:
    """
    Cria um token JWT para autenticação interna
    """
    now = int(time.time())
    expiry = now + (TOKEN_EXPIRY_MINUTES * 60)
    
    # Adicionar timestamps e nonce ao payload
    payload = {
        **data,
        "iat": now,
        "exp": expiry,
        "nonce": secrets.token_hex(8)
    }
    
    # Codificar o payload em base64
    payload_json = json.dumps(payload)
    payload_b64 = base64.b64encode(payload_json.encode()).decode()
    
    # Criar assinatura
    signature = hmac.new(
        JWT_SECRET.encode(),
        payload_b64.encode(),
        hashlib.sha256
    ).digest()
    signature_b64 = base64.b64encode(signature).decode()
    
    # Combinar em um token JWT
    return f"{payload_b64}.{signature_b64}"

def verify_jwt_token(token: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """
    Verifica um token JWT e retorna o payload se válido
    """
    try:
        # Separar payload e assinatura
        if '.' not in token:
            return False, None
            
        payload_b64, signature_b64 = token.split('.')
        
        # Verificar assinatura
        expected_signature = hmac.new(
            JWT_SECRET.encode(),
            payload_b64.encode(),
            hashlib.sha256
        ).digest()
        expected_signature_b64 = base64.b64encode(expected_signature).decode()
        
        if signature_b64 != expected_signature_b64:
            current_app.logger.warning("Assinatura JWT inválida")
            return False, None
            
        # Decodificar payload
        payload_json = base64.b64decode(payload_b64).decode()
        payload = json.loads(payload_json)
        
        # Verificar expiração
        now = int(time.time())
        if payload.get('exp', 0) < now:
            current_app.logger.warning("Token JWT expirado")
            return False, None
            
        # Verificar nonce para prevenir replay attacks
        nonce = payload.get('nonce')
        if nonce in NONCE_STORE:
            current_app.logger.warning(f"Nonce {nonce} já utilizado - possível ataque de replay")
            return False, None
            
        # Registrar nonce usado
        NONCE_STORE[nonce] = now + (TOKEN_EXPIRY_MINUTES * 60)
        
        # Limpar nonces expirados
        expired_nonces = [n for n, exp in NONCE_STORE.items() if exp < now]
        for n in expired_nonces:
            NONCE_STORE.pop(n, None)
            
        return True, payload
        
    except Exception as e:
        current_app.logger.error(f"Erro ao verificar token JWT: {str(e)}")
        return False, None

def get_client_fingerprint() -> str:
    """
    Cria uma impressão digital do cliente combinando diversos fatores
    para identificar clientes únicos além do IP
    """
    user_agent = request.headers.get('User-Agent', '')
    accept_lang = request.headers.get('Accept-Language', '')
    remote_addr = request.remote_addr or '0.0.0.0'
    
    # Extrair informações do user agent
    browser_info = re.findall(r'(?:Chrome|Firefox|Safari|Edge|MSIE)\/[\d\.]+', user_agent)
    browser_string = "".join(browser_info) if browser_info else "unknown"
    
    # Extrair prefixo do IP (3 primeiros octetos para IPv4)
    ip_prefix = ".".join(remote_addr.split('.')[:3]) if '.' in remote_addr else remote_addr
    
    # Criar hash da combinação destes fatores
    fingerprint_data = f"{ip_prefix}|{browser_string}|{accept_lang[:5]}"
    return hashlib.md5(fingerprint_data.encode()).hexdigest()

def check_rate_limit(route_name: str) -> Tuple[bool, Dict[str, Any]]:
    """
    Verifica se um cliente atingiu o limite de taxa para uma rota específica
    
    Retorna: (is_allowed, rate_limit_info)
    """
    # Identificar o cliente
    client_ip = request.remote_addr or '0.0.0.0'
    client_fingerprint = get_client_fingerprint()
    
    # Chave única para este cliente e rota
    key = f"{client_fingerprint}:{route_name}"
    
    # Obter limites para esta rota
    route_limits = API_RATE_LIMITS.get(route_name, API_RATE_LIMITS["default"])
    max_requests = route_limits["count"]
    per_minutes = route_limits["per_minutes"]
    
    # Tempo atual
    now = datetime.now()
    
    # Se o cliente não está no registro, inicializar
    if key not in RATE_LIMIT_STORE:
        RATE_LIMIT_STORE[key] = {
            "count": 1,
            "reset_at": now + timedelta(minutes=per_minutes)
        }
        return True, {
            "limit": max_requests,
            "remaining": max_requests - 1,
            "reset": int((now + timedelta(minutes=per_minutes)).timestamp())
        }
    
    # Verificar se o período foi resetado
    client_data = RATE_LIMIT_STORE[key]
    if now > client_data["reset_at"]:
        # Reiniciar contagem
        RATE_LIMIT_STORE[key] = {
            "count": 1,
            "reset_at": now + timedelta(minutes=per_minutes)
        }
        return True, {
            "limit": max_requests,
            "remaining": max_requests - 1,
            "reset": int((now + timedelta(minutes=per_minutes)).timestamp())
        }
    
    # Verificar se atingiu o limite
    if client_data["count"] >= max_requests:
        current_app.logger.warning(
            f"Taxa limite excedida para {client_ip} ({client_fingerprint}) na rota {route_name}"
        )
        return False, {
            "limit": max_requests,
            "remaining": 0,
            "reset": int(client_data["reset_at"].timestamp())
        }
    
    # Incrementar contador
    client_data["count"] += 1
    
    return True, {
        "limit": max_requests,
        "remaining": max_requests - client_data["count"],
        "reset": int(client_data["reset_at"].timestamp())
    }

def verify_referer() -> bool:
    """
    Verifica se o referer é permitido
    """
    referer = request.headers.get('Referer', '')
    if not referer:
        return False
    
    # Extrair domínio do referer
    try:
        parsed_uri = urlparse(referer)
        referer_domain = parsed_uri.netloc.lower()
        
        # Remover porta do domínio, se houver
        if ':' in referer_domain:
            referer_domain = referer_domain.split(':')[0]
            
        # Verificar se o domínio está na lista de permitidos
        for allowed in ALLOWED_REFERERS:
            if referer_domain == allowed or referer_domain.endswith('.' + allowed):
                return True
                
        current_app.logger.warning(f"Referer não permitido: {referer_domain}")
        return False
    except Exception as e:
        current_app.logger.error(f"Erro ao verificar referer: {str(e)}")
        return False

def verify_csrf_token(token: str) -> bool:
    """
    Verifica se um token CSRF é válido
    """
    clean_expired_csrf_tokens()
    return token in CSRF_TOKENS

def secure_api(route_name: str = None):
    """
    Decorador para proteger rotas de API com múltiplas camadas de segurança
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Determinar o nome da rota
            route = route_name or f.__name__
            
            # 1. Verificar referer
            if not verify_referer():
                current_app.logger.warning(f"Acesso bloqueado à rota {route}: referer inválido")
                abort(403, description="Forbidden: invalid referer")
            
            # 2. Verificar token CSRF para requisições não-GET
            if request.method != 'GET':
                csrf_token = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token')
                if not csrf_token or not verify_csrf_token(csrf_token):
                    current_app.logger.warning(f"Acesso bloqueado à rota {route}: token CSRF inválido")
                    abort(403, description="Forbidden: invalid CSRF token")
            
            # 3. Verificar rate limiting
            allowed, rate_info = check_rate_limit(route)
            if not allowed:
                response = jsonify({
                    "error": "Taxa limite excedida. Tente novamente mais tarde.",
                    "rate_limit": rate_info
                })
                response.status_code = 429
                response.headers['X-RateLimit-Limit'] = str(rate_info['limit'])
                response.headers['X-RateLimit-Remaining'] = str(rate_info['remaining'])
                response.headers['X-RateLimit-Reset'] = str(rate_info['reset'])
                return response
            
            # 4. Para rotas de criação de pagamento, verificar token JWT
            if route in ['create_pix_payment', 'create_discount_payment']:
                auth_token = request.headers.get('X-Auth-Token')
                if not auth_token:
                    current_app.logger.warning(f"Acesso bloqueado à rota {route}: sem token de autenticação")
                    abort(401, description="Unauthorized: authentication token required")
                
                valid, payload = verify_jwt_token(auth_token)
                if not valid:
                    current_app.logger.warning(f"Acesso bloqueado à rota {route}: token inválido")
                    abort(401, description="Unauthorized: invalid authentication token")
            
            # Adicionar headers de rate limit à resposta
            response = f(*args, **kwargs)
            if isinstance(response, tuple):
                response_obj, status_code = response
                headers = {}
            else:
                response_obj = response
                status_code = 200
                headers = {}
            
            # Converter para um objeto de resposta se for um dicionário
            if isinstance(response_obj, dict):
                response_obj = jsonify(response_obj)
            
            # Adicionar headers de rate limit
            response_obj.headers['X-RateLimit-Limit'] = str(rate_info['limit'])
            response_obj.headers['X-RateLimit-Remaining'] = str(rate_info['remaining'])
            response_obj.headers['X-RateLimit-Reset'] = str(rate_info['reset'])
            
            # Adicionar headers de segurança
            response_obj.headers['X-Content-Type-Options'] = 'nosniff'
            response_obj.headers['X-Frame-Options'] = 'DENY'
            response_obj.headers['X-XSS-Protection'] = '1; mode=block'
            
            # Retornar resposta com status code
            if isinstance(response, tuple):
                return response_obj, status_code
            return response_obj
        
        return decorated_function
    return decorator