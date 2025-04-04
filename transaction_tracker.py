import hashlib
import time
from flask import request, current_app
from typing import Dict, Any, Tuple, Optional
from datetime import datetime, timedelta

# Armazenamento de tentativas de transação
# Estrutura: {"IP": {"transaction_hash": {"attempts": int, "last_attempt": timestamp}}}
TRANSACTION_ATTEMPTS = {}

# Limite de transações com os mesmos dados
MAX_TRANSACTION_ATTEMPTS = 5

# Período de retenção para rastreamento de IP
IP_BAN_DURATION = timedelta(hours=24)

# IPs banidos por excesso de tentativas
# Estrutura: {"IP": ban_until_timestamp}
BANNED_IPS = {}

def get_client_ip() -> str:
    """Get client IP address from request"""
    if not request:
        return ""
    
    # Priorizar X-Forwarded-For que é comum em deployments com proxies
    forwarded_for = request.headers.get('X-Forwarded-For')
    if forwarded_for:
        # X-Forwarded-For pode conter múltiplos IPs, pegar o primeiro (cliente real)
        ip = forwarded_for.split(',')[0].strip()
    else:
        # Cair para o IP remoto padrão
        ip = request.remote_addr or "0.0.0.0"
    
    return ip

def hash_transaction_data(data: Dict[str, Any]) -> str:
    """Create a hash from transaction data to identify unique transactions"""
    # Extrair campos relevantes
    relevant_fields = []
    
    # Campos de identificação do cliente
    if 'cpf' in data:
        relevant_fields.append(f"cpf:{data['cpf']}")
    if 'name' in data:
        # Usar apenas o primeiro nome para permitir pequenas variações
        name_parts = str(data.get('name', '')).split()
        if name_parts:
            relevant_fields.append(f"fname:{name_parts[0].lower()}")
    if 'phone' in data and data['phone']:
        # Remover caracteres não numéricos do telefone
        clean_phone = ''.join(filter(str.isdigit, str(data['phone'])))
        relevant_fields.append(f"phone:{clean_phone}")
    
    # Campo de valor - para permitir transações com valores diferentes
    if 'amount' in data:
        amount = float(data['amount'])
        relevant_fields.append(f"amount:{amount:.2f}")
    
    # Criar hash dos campos concatenados
    data_string = "|".join(relevant_fields)
    if not data_string:
        # Se não houver campos relevantes, usar uma string baseada em timestamp
        # para garantir um hash único
        data_string = f"empty_data_{time.time()}"
    
    return hashlib.sha256(data_string.encode()).hexdigest()

def is_transaction_ip_banned(ip: str) -> bool:
    """Check if an IP is banned for transaction attempts"""
    if ip in BANNED_IPS:
        ban_until = BANNED_IPS[ip]
        if datetime.now() < ban_until:
            return True
        else:
            # Remover o banimento expirado
            del BANNED_IPS[ip]
    return False

def track_transaction_attempt(ip: str, data: Dict[str, Any], transaction_id: Optional[str] = None) -> Tuple[bool, str]:
    """
    Track a transaction attempt
    
    Returns:
        Tuple[bool, str]: (is_allowed, message)
    """
    # Verificar se o IP está banido
    if is_transaction_ip_banned(ip):
        return False, f"IP bloqueado por excesso de tentativas. Tente novamente em {IP_BAN_DURATION.total_seconds() / 3600:.1f} horas."
    
    # Criar hash dos dados da transação
    transaction_hash = hash_transaction_data(data)
    
    # Inicializar estrutura para este IP se necessário
    if ip not in TRANSACTION_ATTEMPTS:
        TRANSACTION_ATTEMPTS[ip] = {}
    
    # Verificar tentativas anteriores
    now = datetime.now()
    if transaction_hash in TRANSACTION_ATTEMPTS[ip]:
        # Atualizar contagem de tentativas existentes
        previous = TRANSACTION_ATTEMPTS[ip][transaction_hash]
        
        # Verificar se esta tentativa é para uma transação já iniciada
        if transaction_id and (previous.get("transaction_id") == transaction_id):
            # Esta é uma verificação de status ou continuação legítima, não contar como nova tentativa
            current_app.logger.info(f"Tentativa legítima para transação existente: {transaction_id}")
            return True, "Tentativa legítima para transação existente"
        
        # Verificar limite de tentativas
        if previous["attempts"] >= MAX_TRANSACTION_ATTEMPTS:
            # Adicionar IP à lista de banidos
            BANNED_IPS[ip] = now + IP_BAN_DURATION
            
            # Limpar dados de rastreamento deste IP
            TRANSACTION_ATTEMPTS.pop(ip, None)
            
            current_app.logger.warning(f"IP {ip} bloqueado por excesso de tentativas ({MAX_TRANSACTION_ATTEMPTS}+) com os mesmos dados")
            return False, f"Limite de tentativas excedido. IP bloqueado por {IP_BAN_DURATION.total_seconds() / 3600:.1f} horas."
        
        # Incrementar tentativas e atualizar timestamp
        previous["attempts"] += 1
        previous["last_attempt"] = now
        if transaction_id:
            previous["transaction_id"] = transaction_id
        
        current_app.logger.info(f"Tentativa {previous['attempts']} para hash {transaction_hash[:8]} do IP {ip}")
        return True, f"Tentativa {previous['attempts']} de {MAX_TRANSACTION_ATTEMPTS}"
    else:
        # Registrar nova tentativa
        TRANSACTION_ATTEMPTS[ip][transaction_hash] = {
            "attempts": 1,
            "last_attempt": now,
            "transaction_id": transaction_id
        }
        current_app.logger.info(f"Nova tentativa para hash {transaction_hash[:8]} do IP {ip}")
        return True, "Primeira tentativa registrada"

def cleanup_transaction_tracking():
    """Clean up old transaction tracking data"""
    now = datetime.now()
    expiry_time = now - timedelta(hours=24)
    
    # Limpar IPs banidos
    expired_bans = [ip for ip, ban_until in BANNED_IPS.items() if now > ban_until]
    for ip in expired_bans:
        del BANNED_IPS[ip]
    
    # Limpar tentativas antigas
    for ip in list(TRANSACTION_ATTEMPTS.keys()):
        # Remover transações expiradas para este IP
        expired_transactions = [
            hash_id for hash_id, data in TRANSACTION_ATTEMPTS[ip].items()
            if data["last_attempt"] < expiry_time
        ]
        
        for hash_id in expired_transactions:
            del TRANSACTION_ATTEMPTS[ip][hash_id]
        
        # Remover este IP se não houver mais transações
        if not TRANSACTION_ATTEMPTS[ip]:
            del TRANSACTION_ATTEMPTS[ip]
    
    current_app.logger.info(
        f"Limpeza de rastreamento concluída. IPs banidos: {len(BANNED_IPS)}, IPs rastreados: {len(TRANSACTION_ATTEMPTS)}"
    )