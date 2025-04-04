import hashlib
import time
import re
from flask import request, current_app
from typing import Dict, Any, Tuple, Optional, Set
from datetime import datetime, timedelta

# Armazenamento de tentativas de transação
# Estrutura: {"IP": {"transaction_hash": {"attempts": int, "last_attempt": timestamp}}}
TRANSACTION_ATTEMPTS = {}

# Rastreamento global de nomes/dados de cliente para detectar ataques com múltiplos IPs
# Estrutura: {"client_data_hash": {"ips": set(ips), "attempts": int, "last_attempt": timestamp}}
CLIENT_DATA_TRACKING = {}

# Limite de transações com os mesmos dados
MAX_TRANSACTION_ATTEMPTS = 5  # Por IP
MAX_GLOBAL_CLIENT_ATTEMPTS = 10  # Total global para o mesmo cliente

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

def hash_transaction_data(data: Dict[str, Any], include_amount: bool = True) -> str:
    """
    Create a hash from transaction data to identify unique transactions
    
    Args:
        data: The transaction data
        include_amount: Whether to include the amount in the hash. Set to False to 
                      create a hash only based on client identity (useful for tracking 
                      same client across multiple transactions with different amounts)
    """
    # Extrair campos relevantes
    relevant_fields = []
    
    # Campos de identificação do cliente
    if 'cpf' in data:
        # Normalizar CPF (remover pontos e traços)
        clean_cpf = ''.join(filter(str.isdigit, str(data['cpf'])))
        relevant_fields.append(f"cpf:{clean_cpf}")
    
    if 'name' in data and data['name']:
        # Normalizar nome (converter para minúsculas, remover espaços extras)
        name = str(data['name']).lower().strip()
        
        # Incluir nome completo para melhor precisão
        relevant_fields.append(f"name:{name}")
        
        # Também incluir apenas o primeiro nome para casos de variantes
        name_parts = name.split()
        if name_parts:
            relevant_fields.append(f"fname:{name_parts[0]}")
    
    if 'phone' in data and data['phone']:
        # Normalizar telefone (remover caracteres não numéricos)
        clean_phone = ''.join(filter(str.isdigit, str(data['phone'])))
        if len(clean_phone) > 0:
            relevant_fields.append(f"phone:{clean_phone}")
    
    # Incluir email se disponível (outro identificador forte)
    if 'email' in data and data['email']:
        email = str(data['email']).lower().strip()
        relevant_fields.append(f"email:{email}")
    
    # Campo de valor - para permitir transações com valores diferentes
    if include_amount and 'amount' in data:
        try:
            amount = float(data['amount'])
            relevant_fields.append(f"amount:{amount:.2f}")
        except (ValueError, TypeError):
            # Se não conseguir converter para float, ignorar
            pass
    
    # Criar hash dos campos concatenados
    data_string = "|".join(sorted(relevant_fields))  # Ordenar para garantir consistência
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
    Track a transaction attempt and detect potential attacks from multiple IPs
    using the same client data (proxy attacks)
    
    Returns:
        Tuple[bool, str]: (is_allowed, message)
    """
    # Verificar se o IP está banido
    if is_transaction_ip_banned(ip):
        return False, f"IP bloqueado por excesso de tentativas. Tente novamente em {IP_BAN_DURATION.total_seconds() / 3600:.1f} horas."
    
    # Criar hash dos dados completos da transação (incluindo valor)
    transaction_hash = hash_transaction_data(data, include_amount=True)
    
    # Criar hash apenas dos dados do cliente (sem valor) para rastreamento global
    client_data_hash = hash_transaction_data(data, include_amount=False)
    
    # Inicializar estrutura para este IP se necessário
    if ip not in TRANSACTION_ATTEMPTS:
        TRANSACTION_ATTEMPTS[ip] = {}
    
    # ===== VERIFICAÇÃO DE CLIENTE GLOBAL (DETECTAR ATAQUES COM MÚLTIPLOS IPS) =====
    # Inicializar estrutura para rastreamento global se necessário
    now = datetime.now()
    if client_data_hash not in CLIENT_DATA_TRACKING:
        CLIENT_DATA_TRACKING[client_data_hash] = {
            "ips": set([ip]),
            "attempts": 1,
            "last_attempt": now,
            "first_name": data.get('name', '').split()[0] if data.get('name') else 'Desconhecido'
        }
    else:
        # Atualizar o rastreamento global para este cliente
        client_tracking = CLIENT_DATA_TRACKING[client_data_hash]
        client_tracking["ips"].add(ip)  # Adicionar este IP ao conjunto
        client_tracking["attempts"] += 1
        client_tracking["last_attempt"] = now
        
        # Verificar se este cliente está tentando criar muitas transações usando vários IPs
        if client_tracking["attempts"] > MAX_GLOBAL_CLIENT_ATTEMPTS:
            # Se exceder o limite global, banir todos os IPs associados a este cliente
            for client_ip in client_tracking["ips"]:
                BANNED_IPS[client_ip] = now + IP_BAN_DURATION
                # Limpar dados de rastreamento deste IP
                TRANSACTION_ATTEMPTS.pop(client_ip, None)
            
            # Registrar no log informações detalhadas sobre o bloqueio
            first_name = client_tracking["first_name"]
            ip_count = len(client_tracking["ips"])
            attempt_count = client_tracking["attempts"]
            
            current_app.logger.warning(
                f"ATAQUE DETECTADO: Cliente '{first_name}' detectado usando {ip_count} IPs diferentes "
                f"com {attempt_count} tentativas. Todos os IPs foram banidos."
            )
            
            return False, "Bloqueado por tentativas excessivas usando múltiplos IPs. Tente novamente mais tarde."
    
    # ===== VERIFICAÇÃO DE IP INDIVIDUAL =====
    # Verificar tentativas anteriores deste IP específico com estes dados específicos
    if transaction_hash in TRANSACTION_ATTEMPTS[ip]:
        # Atualizar contagem de tentativas existentes
        previous = TRANSACTION_ATTEMPTS[ip][transaction_hash]
        
        # Verificar se esta tentativa é para uma transação já iniciada
        if transaction_id and (previous.get("transaction_id") == transaction_id):
            # Esta é uma verificação de status ou continuação legítima, não contar como nova tentativa
            current_app.logger.info(f"Tentativa legítima para transação existente: {transaction_id}")
            return True, "Tentativa legítima para transação existente"
        
        # Verificar limite de tentativas para este IP específico
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
        
        # Extrair informações para log de diagnóstico
        name = data.get('name', 'Desconhecido')
        cpf_partial = data.get('cpf', '')[-4:] if data.get('cpf') else 'N/A'
        amount = data.get('amount', 'N/A')
        
        current_app.logger.info(
            f"Nova tentativa para {name} (CPF final {cpf_partial}) no valor {amount} - "
            f"Hash {transaction_hash[:8]} do IP {ip}"
        )
        return True, "Primeira tentativa registrada"

def cleanup_transaction_tracking():
    """Clean up old transaction tracking data"""
    now = datetime.now()
    expiry_time = now - timedelta(hours=24)
    
    # Limpar IPs banidos
    expired_bans = [ip for ip, ban_until in BANNED_IPS.items() if now > ban_until]
    for ip in expired_bans:
        del BANNED_IPS[ip]
    
    # Limpar tentativas antigas de IPs específicos
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
    
    # Limpar rastreamento global de clientes
    expired_clients = [
        client_hash for client_hash, data in CLIENT_DATA_TRACKING.items()
        if data["last_attempt"] < expiry_time
    ]
    
    for client_hash in expired_clients:
        del CLIENT_DATA_TRACKING[client_hash]
    
    # Logging detalhado para monitoramento
    current_app.logger.info(
        f"Limpeza de rastreamento concluída. Estatísticas de proteção: "
        f"IPs banidos: {len(BANNED_IPS)}, "
        f"IPs rastreados: {len(TRANSACTION_ATTEMPTS)}, "
        f"Clientes rastreados globalmente: {len(CLIENT_DATA_TRACKING)}"
    )