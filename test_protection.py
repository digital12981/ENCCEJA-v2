import os
import requests
import time
import json
from concurrent.futures import ThreadPoolExecutor

# URL base da aplicação
BASE_URL = "http://localhost:5000"  # Altere para o seu URL de produção

# Buscar token CSRF
def get_csrf_token():
    """Solicita um novo token CSRF do servidor"""
    try:
        response = requests.get(
            f"{BASE_URL}/csrf-token",
            timeout=5
        )
        
        if response.status_code == 200:
            data = response.json()
            return data.get('csrf_token')
        else:
            print(f"Erro ao obter token CSRF: {response.status_code}")
            print(f"Resposta: {response.text}")
            return None
    except Exception as e:
        print(f"Erro ao solicitar token CSRF: {str(e)}")
        return None

# Dados de pagamento de teste
TEST_PAYMENT_DATA = {
    "name": "Usuário Teste",
    "cpf": "12345678900",
    "amount": 49.90,
    "email": "teste@email.com",
    "phone": "11987654321"
}

def create_payment():
    """Tenta criar um novo pagamento PIX"""
    try:
        # Obter token CSRF primeiro
        csrf_token = get_csrf_token()
        if not csrf_token:
            return 500, "Falha ao obter token CSRF"
            
        # Obter token de pagamento
        token_response = requests.post(
            f"{BASE_URL}/get-payment-token",
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": csrf_token
            },
            timeout=5
        )
        
        if token_response.status_code != 200:
            return token_response.status_code, token_response.text
            
        token_data = token_response.json()
        auth_token = token_data.get('auth_token')
        
        # Criar pagamento usando os tokens
        response = requests.post(
            f"{BASE_URL}/create-pix-payment", 
            json=TEST_PAYMENT_DATA,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {auth_token}",
                "X-CSRF-Token": csrf_token
            },
            timeout=10
        )
        
        print(f"Status: {response.status_code}")
        print(f"Resposta: {response.text}")
        
        return response.status_code, response.text
    except Exception as e:
        print(f"Erro: {str(e)}")
        return 500, str(e)

def test_single_payment():
    """Testa a criação de um único pagamento"""
    print("\n=== Teste Único ===")
    status, response = create_payment()
    
    if status == 200:
        print("✅ Pagamento criado com sucesso!")
    else:
        print(f"❌ Falha ao criar pagamento: {response}")

def test_rate_limiting():
    """Testa o limite de taxa enviando muitas requisições em sequência"""
    print("\n=== Teste de Limite de Taxa ===")
    results = []
    
    for i in range(20):
        print(f"Tentativa {i+1}/20...")
        status, response = create_payment()
        results.append((status, response))
        
        # Pausa curta para não sobrecarregar
        time.sleep(0.5)
    
    # Analisa os resultados
    success = sum(1 for status, _ in results if status == 200)
    rate_limited = sum(1 for status, _ in results if status == 429)
    other_errors = sum(1 for status, _ in results if status not in [200, 429])
    
    print(f"\nResultados:")
    print(f"✅ Sucessos: {success}")
    print(f"🚫 Limitados por taxa: {rate_limited}")
    print(f"❌ Outros erros: {other_errors}")

def test_same_data_blocking():
    """Testa o bloqueio por dados repetidos"""
    print("\n=== Teste de Bloqueio por Dados Repetidos ===")
    
    # Cria várias tentativas com os mesmos dados
    for i in range(10):
        print(f"Tentativa {i+1}/10 com os mesmos dados...")
        status, response = create_payment()
        
        # Se for bloqueado, mostrar a mensagem
        if status != 200:
            try:
                error_data = json.loads(response)
                if 'error' in error_data:
                    print(f"Mensagem: {error_data['error']}")
            except:
                print(f"Resposta: {response}")
                
        time.sleep(1)  # Pausa entre tentativas

def test_concurrent_requests():
    """Testa envio concorrente de muitas requisições"""
    print("\n=== Teste de Requisições Concorrentes ===")
    
    # Número de requisições concorrentes
    num_requests = 10
    
    # Usar ThreadPoolExecutor para enviar requisições em paralelo
    with ThreadPoolExecutor(max_workers=num_requests) as executor:
        futures = [executor.submit(create_payment) for _ in range(num_requests)]
        
    # Aguardar todas as requisições terminarem
    results = [future.result() for future in futures]
    
    # Analisa os resultados
    success = sum(1 for status, _ in results if status == 200)
    rate_limited = sum(1 for status, _ in results if status == 429)
    other_errors = sum(1 for status, _ in results if status not in [200, 429])
    
    print(f"\nResultados:")
    print(f"✅ Sucessos: {success}")
    print(f"🚫 Limitados por taxa: {rate_limited}")
    print(f"❌ Outros erros: {other_errors}")

def run_all_tests():
    """Executa todos os testes em sequência"""
    print("🚀 Iniciando testes de proteção anti-bot...")
    
    # Teste de pagamento único
    test_single_payment()
    
    # Teste de limite de taxa
    test_rate_limiting()
    
    # Teste de bloqueio por dados repetidos
    test_same_data_blocking()
    
    # Teste de requisições concorrentes
    test_concurrent_requests()
    
    print("\n✅ Testes concluídos!")

if __name__ == "__main__":
    run_all_tests()