import os
import cv2
import numpy as np
from flask import Flask, request, jsonify
from flask_cors import CORS
from pyzbar import pyzbar
from pyzbar.pyzbar import decode
from sqlalchemy import create_engine, Column, Integer, String, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
import tempfile
import logging
from werkzeug.utils import secure_filename
import time
from typing import Set, List, Dict, Any

# Configuração de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Inicializar Flask
app = Flask(__name__)
CORS(app)

# Configuração do banco de dados a partir das variáveis de ambiente
DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = os.getenv('DB_PORT', '5432')
DB_NAME = os.getenv('DB_NAME', 'aeroscan_db')
DB_USER = os.getenv('DB_USER', 'aeroscan_user')
DB_PASSWORD = os.getenv('DB_PASSWORD', 'aeroscan_pass')

DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# Configuração SQLAlchemy
Base = declarative_base()
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Modelo de dados para produtos
class Produto(Base):
    __tablename__ = "produtos"
    
    id = Column(Integer, primary_key=True, index=True)
    codigo_barra = Column(String, unique=True, index=True, nullable=False)
    nome_produto = Column(String, nullable=False)
    localizacao = Column(String, nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'codigo_barra': self.codigo_barra,
            'nome_produto': self.nome_produto,
            'localizacao': self.localizacao
        }

def wait_for_db(max_retries: int = 30) -> bool:
    """Aguarda o banco de dados ficar disponível"""
    for attempt in range(max_retries):
        try:
            # Teste simples de conexão
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            logger.info("Conexão com banco de dados estabelecida!")
            return True
        except Exception as e:
            logger.info(f"Tentativa {attempt + 1}/{max_retries}: Aguardando banco de dados... ({e})")
            time.sleep(2)
    
    logger.error("Não foi possível conectar ao banco de dados após várias tentativas")
    return False

def init_database():
    """Inicializa o banco de dados e insere dados de exemplo"""
    if not wait_for_db():
        raise Exception("Falha ao conectar com o banco de dados")
    
    # Criar tabelas
    Base.metadata.create_all(bind=engine)
    logger.info("Tabelas criadas com sucesso!")
    
    # Inserir dados de exemplo se a tabela estiver vazia
    db = SessionLocal()
    try:
        if db.query(Produto).count() == 0:
            produtos_exemplo = [
                Produto(codigo_barra="7891234567890", nome_produto="Produto A", localizacao="Estante 1A"),
                Produto(codigo_barra="7891234567891", nome_produto="Produto B", localizacao="Estante 1B"),
                Produto(codigo_barra="7891234567892", nome_produto="Produto C", localizacao="Estante 2A"),
                Produto(codigo_barra="7891234567893", nome_produto="Produto D", localizacao="Estante 2B"),
                Produto(codigo_barra="1234567890123", nome_produto="Produto E", localizacao="Estante 3A"),
                Produto(codigo_barra="9876543210987", nome_produto="Produto F", localizacao="Estante 3B"),
                Produto(codigo_barra="5555555555555", nome_produto="Produto G", localizacao="Estante 4A"),
                Produto(codigo_barra="1111111111111", nome_produto="Produto H", localizacao="Estante 4B"),
            ]
            
            for produto in produtos_exemplo:
                db.add(produto)
            
            db.commit()
            logger.info(f"Inseridos {len(produtos_exemplo)} produtos de exemplo no banco de dados!")
        else:
            logger.info("Banco de dados já possui dados!")
    
    except Exception as e:
        logger.error(f"Erro ao inicializar dados: {e}")
        db.rollback()
    finally:
        db.close()

def extract_codes_from_video(video_path: str, frame_skip: int = 10) -> Set[str]:
    """
    Extrai códigos de barras/QR codes de um arquivo de vídeo
    
    Args:
        video_path: Caminho para o arquivo de vídeo
        frame_skip: Processar 1 a cada N frames para otimização
    
    Returns:
        Set de códigos únicos encontrados
    """
    codes_found: Set[str] = set()
    
    try:
        # Abrir o vídeo
        cap = cv2.VideoCapture(video_path)
        
        if not cap.isOpened():
            logger.error(f"Não foi possível abrir o vídeo: {video_path}")
            return codes_found
        
        frame_count = 0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        logger.info(f"Processando vídeo com {total_frames} frames...")
        
        while True:
            ret, frame = cap.read()
            
            if not ret:
                break
            
            frame_count += 1
            
            # Processar apenas 1 a cada frame_skip frames
            if frame_count % frame_skip != 0:
                continue
            
            # Converter para escala de cinza para melhor performance
            gray_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            
            # Decodificar códigos de barras/QR codes
            decoded_objects = pyzbar.decode(gray_frame)
            
            for obj in decoded_objects:
                code = obj.data.decode('utf-8')
                codes_found.add(code)
                logger.info(f"Código encontrado no frame {frame_count}: {code}")
        
        cap.release()
        logger.info(f"Processamento concluído! Encontrados {len(codes_found)} códigos únicos.")
        
    except Exception as e:
        logger.error(f"Erro ao processar vídeo: {e}")
    
    return codes_found

def validate_codes_in_database(codes: Set[str]) -> List[Dict[str, Any]]:
    """
    Valida códigos encontrados contra o banco de dados
    
    Args:
        codes: Set de códigos para validar
    
    Returns:
        Lista de dicionários com resultado da validação
    """
    results = []
    db = SessionLocal()
    
    try:
        for code in codes:
            produto = db.query(Produto).filter(Produto.codigo_barra == code).first()
            
            if produto:
                results.append({
                    "codigo": code,
                    "nome_produto": produto.nome_produto,
                    "localizacao": produto.localizacao,
                    "status": "✅ OK"
                })
            else:
                results.append({
                    "codigo": code,
                    "nome_produto": "Não encontrado",
                    "localizacao": "N/A",
                    "status": "❌ Erro"
                })
    
    except Exception as e:
        logger.error(f"Erro ao validar códigos: {e}")
    finally:
        db.close()
    
    return results

# Função para extrair códigos de barras e QR codes de uma imagem
def extract_codes_from_image(filepath: str) -> Dict[str, Any]:
    """
    Processa uma imagem, detecta QR codes e códigos de barras, trata erros e remove o arquivo temporário.
    Args:
        filepath (str): Caminho do arquivo de imagem
    Returns:
        dict: {'qr_codes': [...], 'barcodes': [...], 'error': ...}
    """
    result = {'qr_codes': [], 'barcodes': [], 'validation': [], 'error': None}
    try:
        image = cv2.imread(filepath)
        if image is None:
            os.remove(filepath)
            result['error'] = 'Erro ao ler a imagem.'
            return result
        detected = decode(image)
        qr_codes = [d.data.decode('utf-8') for d in detected if d.type == 'QRCODE']
        barcodes = [d.data.decode('utf-8') for d in detected if d.type != 'QRCODE']
        result['qr_codes'] = qr_codes
        result['barcodes'] = barcodes
        # Validação usando validate_codes_in_database
        all_codes = set(qr_codes + barcodes)
        try:
            validation = validate_codes_in_database(all_codes)
            result['validation'] = validation
        except Exception as e:
            result['error'] = f'Erro ao validar códigos: {str(e)}'
    except Exception as e:
        result['error'] = f'Erro ao processar a imagem: {str(e)}'
    finally:
        try:
            os.remove(filepath)
        except Exception:
            pass
    return result


def extrair_e_validar_codigos(caminho_do_video):
    """
    Extrai códigos de barras/QR codes de um vídeo e valida no banco de dados
    
    Args:
        caminho_do_video (str): Caminho para o arquivo de vídeo
    
    Returns:
        list: Lista de dicionários com os resultados da validação
    """
    # Inicialização das variáveis
    codigos_encontrados = set()  # Conjunto para armazenar códigos únicos
    resultados_finais = []       # Lista para armazenar os resultados finais
    
    # Processamento do Vídeo
    # Abrir o arquivo de vídeo
    cap = cv2.VideoCapture(caminho_do_video)
    
    # Verificar se o vídeo foi aberto corretamente
    if not cap.isOpened():
        logger.error(f"Erro: Não foi possível abrir o vídeo: {caminho_do_video}")
        return resultados_finais
    
    # Inicializar contador de frames
    contador_de_frames = 0
    
    # Loop para ler os frames do vídeo
    while True:
        ret, frame = cap.read()
        
        # Verificar se o vídeo terminou
        if not ret:
            break
        
        # Otimização: processar apenas 1 a cada 15 frames
        if contador_de_frames % 15 == 0:
            # Detectar códigos de barras/QR codes no frame atual
            barcodes = pyzbar.decode(frame)
            
            # Extrair o valor de cada código encontrado
            for barcode in barcodes:
                codigo = barcode.data.decode('utf-8')
                codigos_encontrados.add(codigo)
                logger.info(f"Código encontrado no frame {contador_de_frames}: {codigo}")
        
        # Incrementar contador de frames
        contador_de_frames += 1
    
    # Liberar o recurso do vídeo
    cap.release()
    
    logger.info(f"Processamento de vídeo concluído. Total de códigos únicos encontrados: {len(codigos_encontrados)}")
    
    # Validação no Banco de Dados
    db = SessionLocal()
    
    try:
        # Loop através de cada código encontrado
        for codigo in codigos_encontrados:
            # Consultar o banco de dados para encontrar o produto
            produto = db.query(Produto).filter_by(codigo_barra=codigo).first()
            
            # Verificar se o produto foi encontrado
            if produto is not None:
                # Produto encontrado - criar dicionário de resultado com status OK
                resultado = {
                    'codigo': produto.codigo_barra,
                    'nome_produto': produto.nome_produto,
                    'status': '✅ OK'
                }
            else:
                # Produto não encontrado - criar dicionário de resultado com status Erro
                resultado = {
                    'codigo': codigo,
                    'nome_produto': 'Produto Desconhecido',
                    'status': '❌ Erro'
                }
            
            # Adicionar resultado à lista final
            resultados_finais.append(resultado)
    
    except Exception as e:
        logger.error(f"Erro ao validar códigos no banco de dados: {e}")
    finally:
        # Fechar conexão com o banco
        db.close()
    
    # Retornar lista de resultados
    return resultados_finais

# Rotas da API

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db.close()
        db_status = "OK"
    except Exception as e:
        db_status = f"Erro: {str(e)}"
    
    return jsonify({
        "status": "OK",
        "database": db_status,
        "message": "Olho de Águia Backend está funcionando!"
    })

@app.route('/api/produtos', methods=['GET'])
def listar_produtos():
    """Lista todos os produtos cadastrados"""
    db = SessionLocal()
    try:
        produtos = db.query(Produto).all()
        return jsonify({
            "produtos": [produto.to_dict() for produto in produtos],
            "total": len(produtos)
        })
    except Exception as e:
        logger.error(f"Erro ao listar produtos: {e}")
        return jsonify({"erro": str(e)}), 500
    finally:
        db.close()

@app.route('/api/processar_video', methods=['POST'])
def processar_video():
    """
    Endpoint principal para processar vídeo e encontrar códigos de barras/QR codes
    """
    try:
        # Verificar se foi enviado um arquivo
        if 'video' not in request.files:
            return jsonify({"erro": "Nenhum arquivo de vídeo foi enviado"}), 400
        
        video_file = request.files['video']
        
        if video_file.filename == '':
            return jsonify({"erro": "Nenhum arquivo selecionado"}), 400
        
        # Salvar arquivo temporariamente
        filename = secure_filename(video_file.filename)
        
        # Criar diretório temporário se não existir
        temp_dir = "/app/temp_uploads"
        os.makedirs(temp_dir, exist_ok=True)
        
        temp_path = os.path.join(temp_dir, f"temp_{int(time.time())}_{filename}")
        video_file.save(temp_path)
        
        logger.info(f"Vídeo salvo temporariamente em: {temp_path}")
        
        try:
            # Extrair códigos do vídeo
            codes_found = extract_codes_from_video(temp_path, frame_skip=10)
            
            if not codes_found:
                return jsonify({
                    "message": "Nenhum código de barras ou QR code foi encontrado no vídeo",
                    "codes_found": 0,
                    "results": []
                })
            
            # Validar códigos no banco de dados
            validation_results = validate_codes_in_database(codes_found)
            
            # Contar sucessos e erros
            success_count = len([r for r in validation_results if "✅" in r["status"]])
            error_count = len([r for r in validation_results if "❌" in r["status"]])
            
            return jsonify({
                "message": f"Processamento concluído! Encontrados {len(codes_found)} códigos únicos.",
                "codes_found": len(codes_found),
                "success_count": success_count,
                "error_count": error_count,
                "results": validation_results
            })
        
        finally:
            # Limpar arquivo temporário
            try:
                os.remove(temp_path)
                logger.info(f"Arquivo temporário removido: {temp_path}")
            except Exception as e:
                logger.warning(f"Não foi possível remover arquivo temporário: {e}")
    
    except Exception as e:
        logger.error(f"Erro ao processar vídeo: {e}")
        return jsonify({"erro": f"Erro interno: {str(e)}"}), 500

@app.errorhandler(413)
def too_large(e):
    return jsonify({"erro": "Arquivo muito grande. Tamanho máximo permitido excedido."}), 413

@app.errorhandler(500)
def internal_error(e):
    return jsonify({"erro": "Erro interno do servidor"}), 500

if __name__ == '__main__':
    try:
        logger.info("Iniciando Olho de Águia Backend...")
        
        # Inicializar banco de dados
        init_database()
        
        # Configurar tamanho máximo de upload (100MB)
        app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024
        
        # Iniciar servidor Flask
        app.run(host='0.0.0.0', port=5000, debug=True)
        
    except Exception as e:
        logger.error(f"Erro fatal ao iniciar aplicação: {e}")
        raise
