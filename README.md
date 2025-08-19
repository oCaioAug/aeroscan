# Olho de Águia - Backend

Sistema backend para processamento de vídeos e detecção de códigos de barras/QR codes para hackathon.

## 🚀 Como executar

### Pré-requisitos

- Docker
- Docker Compose

### Execução rápida

```bash
docker-compose up --build
```

Isso irá:

1. Construir a imagem do backend
2. Iniciar o banco PostgreSQL
3. Iniciar o servidor Flask na porta 5000

## 📡 API Endpoints

### Health Check

```
GET /api/health
```

### Listar Produtos

```
GET /api/produtos
```

### Processar Vídeo

```
POST /api/processar_video
Content-Type: multipart/form-data
Body: video (arquivo de vídeo)
```

**Exemplo de resposta:**

```json
{
  "message": "Processamento concluído! Encontrados 3 códigos únicos.",
  "codes_found": 3,
  "success_count": 2,
  "error_count": 1,
  "results": [
    {
      "codigo": "7891234567890",
      "nome_produto": "Produto A",
      "localizacao": "Estante 1A",
      "status": "✅ OK"
    },
    {
      "codigo": "1111111111111",
      "nome_produto": "Produto H",
      "localizacao": "Estante 4B",
      "status": "✅ OK"
    },
    {
      "codigo": "9999999999999",
      "nome_produto": "Não encontrado",
      "localizacao": "N/A",
      "status": "❌ Erro"
    }
  ]
}
```

## 🛠️ Stack Tecnológica

- **Python 3.9+** - Linguagem principal
- **Flask** - Framework web
- **OpenCV** - Processamento de vídeo
- **pyzbar** - Decodificação de códigos de barras/QR
- **PostgreSQL** - Banco de dados
- **SQLAlchemy** - ORM
- **Docker & Docker Compose** - Orquestração

## 📁 Estrutura do Projeto

```
aeroscan/
├── Dockerfile                 # Configuração da imagem Docker
├── docker-compose.yml         # Orquestração dos serviços
├── requirements.txt           # Dependências Python
├── app.py                    # Aplicação Flask principal
└── README.md                 # Este arquivo
```

## 🔧 Desenvolvimento

### Executar apenas o banco

```bash
docker-compose up db
```

### Executar com logs

```bash
docker-compose up --build --logs
```

### Parar os serviços

```bash
docker-compose down
```

### Limpar volumes (CUIDADO: Remove dados do banco)

```bash
docker-compose down -v
```

## 📊 Dados de Exemplo

O sistema inicializa automaticamente com os seguintes produtos de exemplo:

| Código de Barras | Produto   | Localização |
| ---------------- | --------- | ----------- |
| 7891234567890    | Produto A | Estante 1A  |
| 7891234567891    | Produto B | Estante 1B  |
| 7891234567892    | Produto C | Estante 2A  |
| 7891234567893    | Produto D | Estante 2B  |
| 1234567890123    | Produto E | Estante 3A  |
| 9876543210987    | Produto F | Estante 3B  |
| 5555555555555    | Produto G | Estante 4A  |
| 1111111111111    | Produto H | Estante 4B  |

## 🐛 Troubleshooting

### Problema: "Banco não conecta"

- Verifique se as portas não estão em uso
- Aguarde alguns segundos para o PostgreSQL inicializar completamente

### Problema: "Erro ao processar vídeo"

- Verifique se o arquivo é um vídeo válido
- Formatos suportados: MP4, AVI, MOV, etc.
- Tamanho máximo: 100MB

### Logs do sistema

```bash
docker-compose logs backend
docker-compose logs db
```
