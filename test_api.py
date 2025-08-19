#!/usr/bin/env python3
"""
Script de teste para a API do Olho de Águia
Teste os endpoints principais da aplicação
"""

import requests
import json

BASE_URL = "http://localhost:5000"

def test_health():
    """Testa o endpoint de health check"""
    print("🏥 Testando Health Check...")
    try:
        response = requests.get(f"{BASE_URL}/api/health")
        print(f"Status: {response.status_code}")
        print(f"Resposta: {json.dumps(response.json(), indent=2, ensure_ascii=False)}")
        print()
        return response.status_code == 200
    except Exception as e:
        print(f"Erro: {e}")
        return False

def test_produtos():
    """Testa o endpoint de listagem de produtos"""
    print("📦 Testando listagem de produtos...")
    try:
        response = requests.get(f"{BASE_URL}/api/produtos")
        print(f"Status: {response.status_code}")
        data = response.json()
        print(f"Total de produtos: {data.get('total', 0)}")
        
        if data.get('produtos'):
            print("Primeiros 3 produtos:")
            for produto in data['produtos'][:3]:
                print(f"  - {produto['codigo_barra']}: {produto['nome_produto']}")
        print()
        return response.status_code == 200
    except Exception as e:
        print(f"Erro: {e}")
        return False

def test_processar_video():
    """Testa o endpoint de processamento de vídeo (sem arquivo)"""
    print("🎬 Testando processamento de vídeo (sem arquivo)...")
    try:
        response = requests.post(f"{BASE_URL}/api/processar_video")
        print(f"Status: {response.status_code}")
        print(f"Resposta: {json.dumps(response.json(), indent=2, ensure_ascii=False)}")
        print()
        return response.status_code == 400  # Esperamos erro 400 sem arquivo
    except Exception as e:
        print(f"Erro: {e}")
        return False

def main():
    """Executa todos os testes"""
    print("🚀 Iniciando testes da API do Olho de Águia\n")
    print("=" * 50)
    
    tests = [
        ("Health Check", test_health),
        ("Produtos", test_produtos),
        ("Processar Vídeo", test_processar_video)
    ]
    
    results = []
    for name, test_func in tests:
        result = test_func()
        results.append((name, result))
        print("-" * 30)
    
    print("\n📊 RESUMO DOS TESTES:")
    print("=" * 50)
    for name, success in results:
        status = "✅ PASSOU" if success else "❌ FALHOU"
        print(f"{name}: {status}")
    
    total_passed = sum(1 for _, success in results if success)
    print(f"\nTotal: {total_passed}/{len(results)} testes passaram")

if __name__ == "__main__":
    main()
