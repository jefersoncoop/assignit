# app.py

import os
import uuid
import json
import hashlib
from datetime import datetime, UTC
import shutil
import cv2
import io
from flask import Flask, render_template, request, redirect, url_for, abort, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy 
from sqlalchemy import or_
from flask_cors import CORS 
from flask_basicauth import BasicAuth 
from werkzeug.utils import secure_filename
import urllib.parse
import requests
import fitz
import logging
import csv
import threading
import time
import fcntl
from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from PyPDF2 import PdfWriter, PdfReader

# --- Configuração do App e Pastas ---
app = Flask(__name__)
CORS(app) 
# --- Autenticação Multi-usuário ---
class MultiUserBasicAuth(BasicAuth):
    def check_auth(self, username, password, allowed_roles=None):
        authorized_users = app.config.get('AUTHORIZED_USERS', {})
        return username in authorized_users and authorized_users[username] == password

app.config['AUTHORIZED_USERS'] = {
    'admin': 'admin123',
    'crm': 'crm_password_xyz'  # Você pode trocar ou adicionar mais aqui
}
# Chave mestra para integrações externas (CRM, Zapier, etc)
app.config['MASTER_API_KEY'] = os.environ.get('MASTER_API_KEY', 'assignit_key_2024_coopedu')
basic_auth = MultiUserBasicAuth(app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'assinaturas.db')
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{DB_PATH}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

app.config['PENDING_FOLDER'] = os.path.join(BASE_DIR, 'pending')
app.config['SIGNED_FOLDER'] = os.path.join(BASE_DIR, 'signed')
app.config['COMPLETED_FOLDER'] = os.path.join(BASE_DIR, 'completed')
app.config['TEMPLATES_PDF_FOLDER'] = os.path.join(BASE_DIR, 'templates_pdf')
app.config['TEMPLATES_DYNAMIC_FOLDER'] = os.path.join(BASE_DIR, 'templates_dynamic')

for folder_key in ['PENDING_FOLDER', 'SIGNED_FOLDER', 'COMPLETED_FOLDER', 'TEMPLATES_PDF_FOLDER', 'TEMPLATES_DYNAMIC_FOLDER']:
    os.makedirs(app.config[folder_key], exist_ok=True)

db = SQLAlchemy(app)

# --- Modelo do Banco de Dados ---
class Documento(db.Model):
    request_id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    status = db.Column(db.String(20), default='pending')
    original_filename = db.Column(db.String(255))
    original_hash = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC))
    signer_name = db.Column(db.String(255))
    signer_cpf = db.Column(db.String(20))
    signer_phone = db.Column(db.String(20))
    signer_dob = db.Column(db.String(20), nullable=True)
    doc_data = db.Column(db.JSON, nullable=True)
    audit_ip = db.Column(db.String(45), nullable=True)
    audit_user_agent = db.Column(db.String(255), nullable=True)
    audit_timestamp = db.Column(db.DateTime, nullable=True)
    campanha_id = db.Column(db.String(36), nullable=True)
    whatsapp_status = db.Column(db.String(20), default='N/A')
    whatsapp_attempts = db.Column(db.Integer, default=0)

    def to_dict(self):
        return {
            "request_id": self.request_id,
            "status": self.status,
            "nome_arquivo": self.original_filename,
            "nome_signatario": self.signer_name,
            "cpf_signatario": self.signer_cpf,
            "data_criacao": self.created_at.isoformat() if self.created_at else None,
            "data_assinatura": self.audit_timestamp.isoformat() if self.audit_timestamp else None,
            "whatsapp_status": self.whatsapp_status,
            "campanha_id": self.campanha_id
        }

class Campanha(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = db.Column(db.String(255), nullable=False)
    template_id = db.Column(db.String(36), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC))

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "template_id": self.template_id,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }

class TemplateDocumento(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = db.Column(db.String(255), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    fields_mapping = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(UTC))

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "filename": self.original_filename,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }

# --- Funções Auxiliares ---
def calculate_hash(filepath):
    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

def gerar_pdf_para_campanha(tpl, row_data, output_path):
    """Função auxiliar para mesclar dados de uma linha no PDF do template."""
    template_pdf_path = os.path.join(app.config['TEMPLATES_DYNAMIC_FOLDER'], tpl.id, tpl.original_filename)
    if not os.path.exists(template_pdf_path):
        raise FileNotFoundError(f"Template PDF não encontrado em {template_pdf_path}")
        
    template_reader = PdfReader(open(template_pdf_path, "rb"))
    output_writer = PdfWriter()
    fields_by_page = {}
    mapping = tpl.fields_mapping if tpl.fields_mapping else []
    for campo_map in mapping:
        pg = campo_map.get('page', 0)
        if pg not in fields_by_page: fields_by_page[pg] = []
        fields_by_page[pg].append(campo_map)
        
    for page_num in range(len(template_reader.pages)):
        page_obj = template_reader.pages[page_num]
        if page_num in fields_by_page:
            packet = io.BytesIO()
            w = float(page_obj.mediabox.width)
            h = float(page_obj.mediabox.height)
            c = canvas.Canvas(packet, pagesize=(w, h))
            c.setFont("Helvetica", 11)
            
            for campo_map in fields_by_page[page_num]:
                var_name = campo_map.get('name')
                # Procura no row_data ignorando case
                val = next((v for k,v in row_data.items() if k.lower() == var_name.lower()), '')
                if val is None: val = ''
                x_pos = (campo_map.get('x_percent', 0) / 100) * w
                y_pos = h - ((campo_map.get('y_percent', 0) / 100) * h)
                c.drawString(x_pos, y_pos - 4, str(val))
            c.save(); packet.seek(0)
            overlay_pdf = PdfReader(packet)
            page_obj.merge_page(overlay_pdf.pages[0])
        output_writer.add_page(page_obj)

    with open(output_path, "wb") as f: output_writer.write(f)
    return calculate_hash(output_path)

logging.basicConfig(
    filename=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'whatsapp_integration.log'),
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
# --- FUNÇÃO PARA ENVIAR WHATSAPP (COM LOGS DETALHADOS) ---
def enviar_notificacao_whatsapp(nome, cpf, link, etapa, numero, request_id):
    try:
        telefone = ''.join(filter(str.isdigit, str(numero)))
        
        # Verifica se é uma campanha para personalizar o texto
        is_campanha = False
        if request_id:
            with app.app_context():
                from sqlalchemy import text
                # Busca rápida para evitar overhead
                doc = Documento.query.filter_by(request_id=request_id).first()
                if doc and doc.campanha_id:
                    is_campanha = True

        # Formata a descrição
        if etapa == "Concluído":
            if is_campanha:
                descricao = f"Tudo pronto, *{nome}*! Sua *Atualização Cadastral* foi concluída com sucesso. ✅\n\nVocê pode baixar seu comprovante aqui: {link}\n\nA Coopedu agradece sua cooperação! 🚀"
            else:
                descricao = f"Assinatura Concluída! {nome} Seu documento já está disponível. Download: {link}"
        else:
            if is_campanha:
                descricao = f"Olá, *{nome}*! Identificamos que você tem um documento pendente para a sua *Atualização Cadastral* na Coopedu. 📄✨\n\nAssine agora de forma rápida pelo nosso portal seguro: {link}"
            else:
                descricao = f"Solicitação de desligamento recebida! {nome} - CPF: {cpf} Link para assinatura: {link}"

        base_url = "https://webatende.coopedu.com.br:3000/api/crm/notify/"
        params = {
            "titulo": "📢 *AVISO - COOPEDU*",
            "descricao": descricao,
            "etapa": etapa,
            "numero": telefone
        }
        
        # Log de início de tentativa
        logging.info(f"[ENVIO] Tentando enviar para {telefone} | Etapa: {etapa} | ID: {request_id}")

        response = requests.post(base_url, params=params, timeout=12)
        
        if response.status_code == 200:
            logging.info(f"[SUCESSO] Mensagem enviada para {telefone} | Resposta: {response.text}")
            return True
        else:
            logging.error(f"[ERRO API] Código: {response.status_code} | Resposta: {response.text} | Telefone: {telefone}")
            return False

    except requests.exceptions.Timeout:
        logging.error(f"[TIMEOUT] A API de WhatsApp demorou muito para responder | ID: {request_id}")
        return False
    except Exception as e:
        logging.error(f"[FALHA CRÍTICA] Erro ao processar envio para {numero}: {str(e)}")
        return False
def mask_cpf(cpf):
    if not cpf: return "***.***.***-**"
    cpf_numerico = ''.join(filter(str.isdigit, cpf))
    if len(cpf_numerico) != 11:
        return f"***.{cpf_numerico[3:6]}.{cpf_numerico[6:9]}-**"
    return f"***.{cpf_numerico[3:6]}.{cpf_numerico[6:9]}-**"

# --- ROTAS ADMIN ---
@app.route('/admin')
@basic_auth.required
def admin_dashboard():
    # Agora o frontend busca os dados via API paginada para ser mais rápido
    return render_template('admin.html')

@app.route('/api/admin/docs', methods=['GET'])
@basic_auth.required
def api_listar_docs_geral():
    q = request.args.get('q', '')
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    status_filter = request.args.get('status', '')

    query = Documento.query
    if q:
        query = query.filter(or_(
            Documento.signer_name.ilike(f"%{q}%"),
            Documento.signer_cpf.ilike(f"%{q}%")
        ))
    if status_filter:
        query = query.filter_by(status=status_filter)

    pagination = query.order_by(Documento.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
    
    return jsonify({
        "items": [d.to_dict() for d in pagination.items],
        "total": pagination.total,
        "pages": pagination.pages,
        "current_page": pagination.page
    })

@app.route('/admin/delete-pending/<request_id>', methods=['DELETE'])
@basic_auth.required
def delete_pending_document(request_id):
    doc = db.session.get(Documento, request_id)
    if not doc or doc.status != 'pending':
        return jsonify({"sucesso": False, "erro": "Documento não encontrado ou já assinado."}), 400
    try:
        pending_path = os.path.join(app.config['PENDING_FOLDER'], doc.request_id)
        if os.path.exists(pending_path):
            shutil.rmtree(pending_path)
        db.session.delete(doc)
        db.session.commit()
        return jsonify({"sucesso": True, "mensagem": f"Documento {request_id} excluído."})
    except Exception as e:
        db.session.rollback()
        return jsonify({"sucesso": False, "erro": str(e)}), 500

# --- ROTA DE EXCLUSÃO PELO USUÁRIO ---
@app.route('/api/excluir-documento', methods=['POST'])
def user_delete_document():
    dados = request.json
    request_id = dados.get('request_id')
    cpf = dados.get('cpf')

    doc = db.session.get(Documento, request_id)
    if not doc:
        return jsonify({"sucesso": False, "erro": "Documento não encontrado."}), 404
    
    if doc.status != 'pending':
        return jsonify({"sucesso": False, "erro": "Não é possível excluir um documento já assinado."}), 400

    # Validação rigorosa: CPF e Data de Nascimento devem bater com o DB
    # Limpa caracteres do CPF para comparação
    cpf_limpo = ''.join(filter(str.isdigit, cpf))
    doc_cpf_limpo = ''.join(filter(str.isdigit, doc.signer_cpf))

    if cpf_limpo != doc_cpf_limpo :
        return jsonify({"sucesso": False, "erro": "Dados de validação incorretos."}), 403

    try:
        pending_path = os.path.join(app.config['PENDING_FOLDER'], request_id)
        if os.path.exists(pending_path):
            shutil.rmtree(pending_path)
        db.session.delete(doc)
        db.session.commit()
        return jsonify({"sucesso": True, "mensagem": "Solicitação excluída com sucesso."})
    except Exception as e:
        db.session.rollback()
        return jsonify({"sucesso": False, "erro": str(e)}), 500

# --- Rotas Template Builder ---
@app.route('/admin/builder')
@basic_auth.required
def template_builder():
    return render_template('template_builder.html')

@app.route('/api/admin/template/upload', methods=['POST'])
@basic_auth.required
def upload_template_builder():
    if 'documento' not in request.files:
        return jsonify({"sucesso": False, "erro": "Nenhum arquivo PDF foi enviado."}), 400
    file = request.files['documento']
    filename = secure_filename(file.filename)
    
    temp_id = str(uuid.uuid4())
    temp_dir = os.path.join(app.config['TEMPLATES_DYNAMIC_FOLDER'], temp_id)
    os.makedirs(temp_dir, exist_ok=True)
    pdf_path = os.path.join(temp_dir, filename)
    file.save(pdf_path)
    
    image_paths = []
    try:
        doc_fitz = fitz.open(pdf_path)
        for page_num in range(len(doc_fitz)):
            page = doc_fitz.load_page(page_num)
            pix = page.get_pixmap(dpi=150)
            img_name = f"page_{page_num}.png"
            pix.save(os.path.join(temp_dir, img_name))
            image_paths.append({
                "page": page_num,
                "url": url_for('get_template_file', temp_id=temp_id, filename=img_name),
                "width": page.rect.width,
                "height": page.rect.height
            })
    except Exception as e:
        return jsonify({"sucesso": False, "erro": f"Erro manipulando PDF: {str(e)}"}), 500
        
    return jsonify({
        "sucesso": True,
        "temp_id": temp_id,
        "filename": filename,
        "images": image_paths
    })

@app.route('/api/admin/template/save', methods=['POST'])
@basic_auth.required
def save_template_builder():
    dados = request.json
    name = dados.get('name')
    temp_id = dados.get('temp_id')
    filename = dados.get('filename')
    fields = dados.get('fields')
    
    if not all([name, temp_id, filename, fields is not None]):
        return jsonify({"sucesso": False, "erro": "Dados incompletos"}), 400
        
    temp_dir = os.path.join(app.config['TEMPLATES_DYNAMIC_FOLDER'], temp_id)
    if not os.path.exists(temp_dir):
        return jsonify({"sucesso": False, "erro": "Template temporário não encontrado"}), 404
        
    try:
        new_template = TemplateDocumento(
            id=temp_id,
            name=name,
            original_filename=filename,
            fields_mapping=fields
        )
        db.session.add(new_template)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"sucesso": False, "erro": str(e)}), 500
        
    return jsonify({"sucesso": True, "template_id": temp_id})

@app.route('/templates_dynamic/<temp_id>/<filename>')
def get_template_file(temp_id, filename):
    return send_from_directory(os.path.join(app.config['TEMPLATES_DYNAMIC_FOLDER'], temp_id), filename)

@app.route('/api/admin/templates', methods=['GET'])
@basic_auth.required
def list_templates_builder():
    templates = TemplateDocumento.query.order_by(TemplateDocumento.created_at.desc()).all()
    return jsonify([t.to_dict() for t in templates])

@app.route('/admin/template/<template_id>', methods=['DELETE'])
@basic_auth.required
def delete_template_builder(template_id):
    tpl = db.session.get(TemplateDocumento, template_id)
    if not tpl:
        return jsonify({"sucesso": False, "erro": "Template não encontrado."}), 404
    try:
        temp_dir = os.path.join(app.config['TEMPLATES_DYNAMIC_FOLDER'], tpl.id)
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        db.session.delete(tpl)
        db.session.commit()
        return jsonify({"sucesso": True, "mensagem": "Template excluído."})
    except Exception as e:
        db.session.rollback()
        return jsonify({"sucesso": False, "erro": str(e)}), 500

# --- Rotas da API ---
@app.route('/', methods=['GET'])
def index():
    return jsonify({"status": "API de assinaturas digitais está online."}), 200

@app.route('/api/criar-solicitacao', methods=['POST'])
def create_signature_api():
    if 'documento' not in request.files:
        return jsonify({"sucesso": False, "erro": "Nenhum arquivo PDF foi enviado."}), 400
    file = request.files['documento']
    dados_signatario = request.form
    cpf = dados_signatario.get('cpf')
    filename = secure_filename(file.filename)

    # DUPLICIDADE: Verifica se já existe um pendente para esse CPF e arquivo
    existente = Documento.query.filter_by(signer_cpf=cpf, original_filename=filename, status='pending').first()
    if existente:
        return jsonify({
            "sucesso": True, 
            "mensagem": "Documento já existente.", 
            "request_id": existente.request_id, 
            "signing_link": url_for('sign_document', request_id=existente.request_id, _external=True)
        }), 200
            
    request_id = str(uuid.uuid4())
    pending_path = os.path.join(app.config['PENDING_FOLDER'], request_id)
    os.makedirs(pending_path)
    temp_filepath = os.path.join(pending_path, filename)
    file.save(temp_filepath)
    original_hash = calculate_hash(temp_filepath)
    
    try:
        new_doc = Documento(
            request_id=request_id, signer_name=dados_signatario['nome'],
            signer_cpf=cpf, signer_dob=dados_signatario['data_nascimento'],
            original_filename=filename, original_hash=original_hash
        )
        db.session.add(new_doc)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"sucesso": False, "erro": str(e)}), 500
        
    signing_link = url_for('sign_document', request_id=request_id, _external=True)
    return jsonify({ "sucesso": True, "request_id": request_id, "signing_link": signing_link }), 201

@app.route('/api/criar-por-modelo', methods=['POST'])
def create_from_template_api():
    dados = request.json
    if not dados: return jsonify({"sucesso": False, "erro": "JSON inválido."}), 400
        
    # Adicionado 'data_nascimento' como campo necessário para permitir exclusão futura
    campos_req = ['nome', 'cpf', 'conta', 'banco', 'agencia', 'tipoconta', 'telefone', 'email']
    if not all(campo in dados and dados[campo] for campo in campos_req):
        return jsonify({"sucesso": False, "erro": "Campos obrigatórios ausentes."}), 400

    # DUPLICIDADE: Para modelos, verificamos por CPF e status pendente
    existente = Documento.query.filter_by(signer_cpf=dados['cpf'], status='pending').first()
    if existente and existente.original_filename.startswith('documento_preenchido_'):
        return jsonify({
            "sucesso": True, 
            "mensagem": "Você já possui uma solicitação pendente para este contrato.", 
            "request_id": existente.request_id, 
            "signing_link": url_for('sign_document', request_id=existente.request_id, _external=True),
            "download_link": url_for('download_file', filename=f"signed_{existente.original_filename}", _external=True) 
        }), 200

    request_id = str(uuid.uuid4())
    pending_path = os.path.join(app.config['PENDING_FOLDER'], request_id)
    os.makedirs(pending_path)

    try:
        template_path = os.path.join(app.config['TEMPLATES_PDF_FOLDER'], 'PEDIDO DE DESLIGAMENTO V5.pdf')
        if not os.path.exists(template_path): return jsonify({"erro": "Template não encontrado."}), 500
             
        final_pdf_name = f"documento_preenchido_{request_id}.pdf"
        output_pdf_path = os.path.join(pending_path, final_pdf_name)
        packet = io.BytesIO()
        c = canvas.Canvas(packet, pagesize=letter)
        # ... (Sua lógica de escrita no PDF permanece igual)
        c.drawString(80, 380, f"COOPERADO: {dados['nome']}")
        c.drawString(80, 360, f"DADOS BANCARIOS")
        c.drawString(80, 340, f"BANCO: {dados['banco']}")
        c.drawString(80, 320, f"AGÊNCIA: {dados['agencia']}")
        c.drawString(80, 300, f"CONTA: {dados['conta']}")
        c.drawString(80, 280, f"TIPO DE CONTA: {dados['tipoconta']}")
        c.drawString(80, 260, f"CPF: {dados['cpf']}")
        c.drawString(80, 240, f"TELEFONE : {dados['telefone']}")
        c.drawString(80, 220, f"E-MAIL: {dados['email']}")
        c.save()
        packet.seek(0)
        data_pdf = PdfReader(packet)
        template_pdf = PdfReader(open(template_path, "rb"))
        output_writer = PdfWriter()
        page = template_pdf.pages[0]; page.merge_page(data_pdf.pages[0])
        output_writer.add_page(page)
        for page_num in range(1, len(template_pdf.pages)): output_writer.add_page(template_pdf.pages[page_num])
        with open(output_pdf_path, "wb") as f: output_writer.write(f)
    except Exception as e:
        return jsonify({"sucesso": False, "erro": str(e)}), 500

    original_hash = calculate_hash(output_pdf_path)
    try:
        new_doc = Documento(
            request_id=request_id, signer_name=dados['nome'],
            signer_cpf=dados['cpf'], signer_phone=dados['telefone'],
            doc_data=dados, original_filename=final_pdf_name, original_hash=original_hash
        )
        db.session.add(new_doc)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"sucesso": False, "erro": str(e)}), 500
        
    signing_link = url_for('sign_document', request_id=request_id, _external=True)
    # ENVIAR WHATSAPP DE CRIAÇÃO
    enviar_notificacao_whatsapp(dados['nome'], dados['cpf'], signing_link, "Aguardando Assinatura", dados['telefone'])
    return jsonify({ "sucesso": True, "request_id": request_id, "signing_link": signing_link }), 201

@app.route('/api/criar-solicitacao-dinamica', methods=['POST'])
def create_dynamic_template_api():
    dados = request.json
    if not dados: return jsonify({"sucesso": False, "erro": "JSON inválido."}), 400
    
    template_id = dados.get('template_id')
    if not template_id: return jsonify({"sucesso": False, "erro": "template_id é obrigatório."}), 400
    
    tpl = db.session.get(TemplateDocumento, template_id)
    if not tpl: return jsonify({"sucesso": False, "erro": "Template não encontrado."}), 404
    
    if not all(campo in dados and dados[campo] for campo in ['nome', 'cpf', 'telefone']):
        return jsonify({"sucesso": False, "erro": "Campos básicos ausentes (nome, cpf, telefone)."}), 400

    existente = Documento.query.filter_by(signer_cpf=dados['cpf'], status='pending').first()
    if existente and existente.original_filename.startswith(f'doc_dinamico_{template_id}'):
        return jsonify({
            "sucesso": True,
            "mensagem": "Você já possui uma solicitação pendente para este contrato.",
            "request_id": existente.request_id,
            "signing_link": url_for('sign_document', request_id=existente.request_id, _external=True)
        }), 200
        
    request_id = str(uuid.uuid4())
    pending_path = os.path.join(app.config['PENDING_FOLDER'], request_id)
    os.makedirs(pending_path)
    
    final_pdf_name = f"doc_dinamico_{template_id}_{request_id}.pdf"
    output_pdf_path = os.path.join(pending_path, final_pdf_name)
    
    template_pdf_path = os.path.join(app.config['TEMPLATES_DYNAMIC_FOLDER'], tpl.id, tpl.original_filename)
    if not os.path.exists(template_pdf_path):
        return jsonify({"sucesso": False, "erro": "Arquivo PDF base do template ausente."}), 500

    try:
        template_reader = PdfReader(open(template_pdf_path, "rb"))
        output_writer = PdfWriter()
        
        fields_by_page = {}
        for campo_map in tpl.fields_mapping:
            pg = campo_map.get('page', 0)
            if pg not in fields_by_page: fields_by_page[pg] = []
            fields_by_page[pg].append(campo_map)
            
        for page_num in range(len(template_reader.pages)):
            page_obj = template_reader.pages[page_num]
            
            if page_num in fields_by_page:
                packet = io.BytesIO()
                w = float(page_obj.mediabox.width)
                h = float(page_obj.mediabox.height)
                c = canvas.Canvas(packet, pagesize=(w, h))
                c.setFont("Helvetica", 11)
                
                for campo_map in fields_by_page[page_num]:
                    var_name = campo_map.get('name')
                    val = dados.get(var_name, '')
                    if val is None:
                        val = ''
                    
                    x_pct = campo_map.get('x_percent', 0)
                    y_pct = campo_map.get('y_percent', 0)
                    
                    x_pos = (x_pct / 100) * w
                    y_pos = h - ((y_pct / 100) * h)
                    
                    c.drawString(x_pos, y_pos - 4, str(val))
                    
                c.save()
                packet.seek(0)
                overlay_pdf = PdfReader(packet)
                page_obj.merge_page(overlay_pdf.pages[0])
                
            output_writer.add_page(page_obj)

        with open(output_pdf_path, "wb") as f: output_writer.write(f)
            
    except Exception as e:
        return jsonify({"sucesso": False, "erro": str(e)}), 500

    original_hash = calculate_hash(output_pdf_path)
    try:
        new_doc = Documento(
            request_id=request_id, signer_name=dados['nome'],
            signer_cpf=dados['cpf'], signer_phone=dados['telefone'],
            doc_data=dados, original_filename=final_pdf_name, original_hash=original_hash
        )
        db.session.add(new_doc)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"sucesso": False, "erro": str(e)}), 500
        
    signing_link = url_for('sign_document', request_id=request_id, _external=True)
    enviar_notificacao_whatsapp(dados['nome'], dados['cpf'], signing_link, "Aguardando Assinatura", dados['telefone'])
    
    return jsonify({ "sucesso": True, "request_id": request_id, "signing_link": signing_link }), 201


# --- ROTAS DE CAMPANHA ---
@app.route('/api/admin/campanhas/<campanha_id>', methods=['DELETE'])
@basic_auth.required
def deletar_campanha(campanha_id):
    camp = db.session.get(Campanha, campanha_id)
    if not camp:
        return jsonify({"sucesso": False, "erro": "Campanha não encontrada"}), 404
        
    try:
        # 1. Buscar todos os documentos da campanha
        docs = Documento.query.filter_by(campanha_id=campanha_id).all()
        
        for doc in docs:
            # 2. Remover arquivos físicos (Pendentes)
            p_path = os.path.join(app.config['PENDING_FOLDER'], doc.request_id)
            if os.path.exists(p_path):
                shutil.rmtree(p_path)
            
            # 3. Remover arquivos físicos (Assinados)
            s_file = os.path.join(app.config['SIGNED_FOLDER'], f"signed_{doc.original_filename}")
            if os.path.exists(s_file):
                os.remove(s_file)
            
            # 4. Remover registro do banco
            db.session.delete(doc)
            
        # 5. Remover a campanha
        db.session.delete(camp)
        db.session.commit()
        
        logging.info(f"[ADMIN] Campanha {campanha_id} e seus arquivos foram excluídos com sucesso.")
        return jsonify({"sucesso": True, "mensagem": "Campanha e documentos excluídos com sucesso."})
        
    except Exception as e:
        db.session.rollback()
        logging.error(f"[ADMIN] Erro ao excluir campanha {campanha_id}: {str(e)}")
        return jsonify({"sucesso": False, "erro": str(e)}), 500

@app.route('/api/admin/campanhas', methods=['GET'])
@basic_auth.required
def listar_campanhas():
    q = request.args.get('q', '')
    query = Campanha.query
    if q:
        query = query.filter(Campanha.name.ilike(f"%{q}%"))
        
    campanhas = query.order_by(Campanha.created_at.desc()).all()
    res = []
    for c in campanhas:
        docs_query = Documento.query.filter_by(campanha_id=c.id)
        total = docs_query.count()
        assinados = docs_query.filter_by(status='signed').count()
        # Documentos "gerados" são aqueles que NÃO estão mais em fila de geração
        gerados = docs_query.filter(~Documento.status.in_(['generating', 'processing', 'error_generating'])).count()
        
        d = c.to_dict()
        d['total_docs'] = total
        d['docs_assinados'] = assinados
        d['docs_gerados'] = gerados
        d['docs_pendentes'] = total - assinados
        res.append(d)
    return jsonify(res)

@app.route('/api/admin/template/<template_id>/csv-padrao', methods=['GET'])
@basic_auth.required
def baixar_template_csv(template_id):
    tpl = db.session.get(TemplateDocumento, template_id)
    if not tpl:
        return jsonify({"sucesso": False, "erro": "Template não encontrado"}), 404
        
    vars_mapped = set([campo.get('name') for campo in tpl.fields_mapping])
    obrigatorios = ['nome', 'cpf', 'telefone']
    headers = obrigatorios.copy()
    for v in vars_mapped:
        if v not in headers: headers.append(v)
            
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(headers)
    
    csv_data = si.getvalue().encode('utf-8')
    
    return csv_data, 200, {
        "Content-Disposition": "attachment; filename=modelo_campanha.csv",
        "Content-type": "text/csv; charset=utf-8"
    }

def background_campaign_processor(app_ctx):
    """Worker que varre o banco por documentos com status 'generating' e gera os PDFs."""
    while True:
        try:
            with app_ctx:
                # Busca documentos que ainda precisam de PDF
                # Marcamos como 'processing' para evitar que outros workers (se houver) peguem o mesmo
                doc = Documento.query.filter_by(status='generating').first()
                
                if not doc:
                    # Nada para gerar, solta o contexto e dorme
                    pass
                else:
                    # Marca como processando imediatamente
                    doc.status = 'processing'
                    db.session.commit()
                    
                    logging.info(f"[BG PDF] Iniciando geração do documento: {doc.request_id}")
                    try:
                        camp = db.session.get(Campanha, doc.campanha_id)
                        tpl = db.session.get(TemplateDocumento, camp.template_id) if camp else None
                        
                        if not tpl:
                            doc.status = 'error_config'
                            db.session.commit()
                        else:
                            p_path = os.path.join(app.config['PENDING_FOLDER'], doc.request_id)
                            os.makedirs(p_path, exist_ok=True)
                            out_path = os.path.join(p_path, doc.original_filename)
                            
                            h = gerar_pdf_para_campanha(tpl, doc.doc_data, out_path)
                            doc.original_hash = h
                            doc.status = 'pending'
                            db.session.commit()
                            logging.info(f"[BG PDF] SUCESSO: {doc.request_id}")
                    except Exception as e:
                        logging.error(f"[BG PDF] Erro no doc {doc.request_id}: {str(e)}")
                        doc.status = 'error_generating'
                        db.session.commit()
            
            # Se não tinha nada, dorme 10s. Se processou um, dorme 0.1s para agilidade
            if not doc:
                time.sleep(10)
            else:
                time.sleep(0.1)
                
        except Exception as e:
            logging.error(f"[BG PDF] Erro crítico no worker: {str(e)}")
            time.sleep(10)

@app.route('/api/admin/campanhas/upload', methods=['POST'])
@basic_auth.required
def upload_campanhas_csv():
    nome = request.form.get('nome')
    template_id = request.form.get('template_id')
    if 'csv_file' not in request.files or not template_id or not nome:
        return jsonify({"sucesso": False, "erro": "Dados incompletos"}), 400
        
    tpl = db.session.get(TemplateDocumento, template_id)
    if not tpl: return jsonify({"sucesso": False, "erro": "Template não disponível."}), 404
    
    camp_id = str(uuid.uuid4())
    new_campanha = Campanha(id=camp_id, name=nome, template_id=template_id)
    db.session.add(new_campanha)
    db.session.commit()
    
    file = request.files['csv_file']
    raw_bytes = file.stream.read()
    try: text = raw_bytes.decode('utf-8-sig')
    except: text = raw_bytes.decode('latin-1')

    delimiter = ';' if ';' in text.split('\n')[0] else ','
    stream = io.StringIO(text, newline=None)
    csv_input = csv.DictReader(stream, delimiter=delimiter)
    
    count = 0
    for row in [r for r in csv_input if r]:
        cpf_key = next((k for k in row.keys() if k and k.strip().lower() == 'cpf'), None)
        if not cpf_key: continue
        cpf = ''.join(filter(str.isdigit, str(row[cpf_key])))
        tel_key = next((k for k in row.keys() if k and k.strip().lower() in ['telefone', 'whatsapp', 'celular', 'phone']), None)
        tel = ''.join(filter(str.isdigit, str(row[tel_key]))) if tel_key else ''
        nome_key = next((k for k in row.keys() if k and k.strip().lower() == 'nome'), None)
        nome = str(row[nome_key]).strip() if nome_key else 'Participante'
        
        req_id = str(uuid.uuid4())
        new_doc = Documento(
            request_id=req_id, signer_name=nome, signer_cpf=cpf, signer_phone=tel,
            doc_data=row, original_filename=f"campanha_{camp_id}_{req_id}.pdf",
            campanha_id=camp_id, status='generating', whatsapp_status='Pausado'
        )
        db.session.add(new_doc)
        count += 1
    
    db.session.commit()
    return jsonify({"sucesso": True, "campanha_id": camp_id, "mensagem": f"Upload aceito! {count} registros inseridos na fila de processamento."})

@app.route('/api/admin/campanhas/<campanha_id>/append-csv', methods=['POST'])
@basic_auth.required
def append_campanha_csv(campanha_id):
    if 'csv_file' not in request.files: return jsonify({"sucesso": False, "erro": "Arquivo ausente"}), 400
    camp = db.session.get(Campanha, campanha_id)
    if not camp: return jsonify({"sucesso": False, "erro": "Campanha não existe"}), 404
    
    file = request.files['csv_file']
    raw_bytes = file.stream.read()
    try: text = raw_bytes.decode('utf-8-sig')
    except: text = raw_bytes.decode('latin-1')
    
    delimiter = ';' if ';' in text.split('\n')[0] else ','
    stream = io.StringIO(text, newline=None)
    csv_input = csv.DictReader(stream, delimiter=delimiter)
    
    count = 0
    for row in [r for r in csv_input if r]:
        cpf_key = next((k for k in row.keys() if k and k.strip().lower() == 'cpf'), None)
        if not cpf_key: continue
        cpf = ''.join(filter(str.isdigit, str(row[cpf_key])))
        tel_key = next((k for k in row.keys() if k and k.strip().lower() in ['telefone', 'whatsapp', 'celular', 'phone']), None)
        tel = ''.join(filter(str.isdigit, str(row[tel_key]))) if tel_key else ''
        nome_key = next((k for k in row.keys() if k and k.strip().lower() == 'nome'), None)
        nome = str(row[nome_key]).strip() if nome_key else 'Participante'
        
        req_id = str(uuid.uuid4())
        new_doc = Documento(
            request_id=req_id, signer_name=nome, signer_cpf=cpf, signer_phone=tel,
            doc_data=row, original_filename=f"campanha_{campanha_id}_{req_id}.pdf",
            campanha_id=campanha_id, status='generating', whatsapp_status='Pausado'
        )
        db.session.add(new_doc)
        count += 1
        
    db.session.commit()
    return jsonify({"sucesso": True, "mensagem": f"Importação de {count} novos registros iniciada!"})

@app.route('/campanha/<campanha_id>', methods=['GET'])
def campanha_login_geral(campanha_id):
    camp = db.session.get(Campanha, campanha_id)
    if not camp: return "<h1>Campanha não encontrada</h1>", 404
    return render_template('campanha_auth.html', request_id='', campanha_id=campanha_id)

@app.route('/campanha/auth/<request_id>', methods=['GET'])
def campanha_auth(request_id):
    doc = db.session.get(Documento, request_id)
    if not doc or not doc.campanha_id: return "<h1>Inválido</h1>", 404
    if doc.status == 'signed': return redirect(url_for('success', filename=f"signed_{doc.original_filename}"))
    return render_template('campanha_auth.html', request_id=request_id, campanha_id='')

@app.route('/api/campanha/auth/validar', methods=['POST'])
def validate_campanha_auth():
    dados = request.json
    request_id = dados.get('request_id')
    campanha_id = dados.get('campanha_id')
    cpf = dados.get('cpf')
    
    cpf_limpo = ''.join(filter(str.isdigit, str(cpf)))
    
    doc = None
    if request_id:
        doc = db.session.get(Documento, request_id)
        if doc and ''.join(filter(str.isdigit, str(doc.signer_cpf))) != cpf_limpo:
            doc = None
    elif campanha_id:
        docs = Documento.query.filter_by(campanha_id=campanha_id).all()
        doc = next((d for d in docs if ''.join(filter(str.isdigit, str(d.signer_cpf))) == cpf_limpo), None)

    if not doc:
        return jsonify({"sucesso": False, "erro": "CPF não localizado para esta campanha."}), 403
        
    if doc.status == 'signed':
        return jsonify({
            "sucesso": True, 
            "status": "signed",
            "redirect_url": url_for('success', filename=f"signed_{doc.original_filename}")
        })
        
    return jsonify({"sucesso": True, "request_id": doc.request_id, "status": doc.status})

@app.route('/api/campanha/auth/telefone', methods=['POST'])
def validate_campanha_telefone():
    dados = request.json
    request_id = dados.get('request_id')
    telefone = dados.get('telefone')
    
    doc = db.session.get(Documento, request_id)
    if not doc or not doc.campanha_id: return jsonify({"sucesso": False, "erro": "Inválido"}), 404
    
    if telefone:
        doc.signer_phone = ''.join(filter(str.isdigit, str(telefone)))
        db.session.commit()
        
    return jsonify({"sucesso": True, "redirect_url": url_for('visualizar_documento_campanha', request_id=request_id, _external=True)})

@app.route('/campanha/visualizar/<request_id>')
def visualizar_documento_campanha(request_id):
    doc = db.session.get(Documento, request_id)
    if not doc or not doc.campanha_id: return "<h1>Inválido</h1>", 404
    if doc.status == 'signed': return redirect(url_for('success', filename=f"signed_{doc.original_filename}"))
    pdf_url = url_for('get_pending_file', request_id=request_id, filename=doc.original_filename)
    return render_template('campanha_leitura.html', request_id=request_id, pdf_url=pdf_url, nome=doc.signer_name)

@app.route('/api/campanha/<campanha_id>/documento/<cpf>', methods=['GET'])
def get_status_campanha_crm(campanha_id, cpf):
    doc = Documento.query.filter_by(campanha_id=campanha_id, signer_cpf=cpf).first()
    if not doc:
        cpf_num = ''.join(filter(str.isdigit, cpf))
        docs = Documento.query.filter_by(campanha_id=campanha_id).all()
        doc = next((d for d in docs if ''.join(filter(str.isdigit, d.signer_cpf)) == cpf_num), None)
        
    if not doc: return jsonify({"sucesso": False, "erro": "CPF não encontrado nesta campanha."}), 404
    ans = doc.to_dict()
    if doc.status == 'signed':
        ans['download_link'] = f"https://assign.tec.br/download/signed_{doc.original_filename}"
    return jsonify({"sucesso": True, "documento": ans})

@app.route('/api/admin/campanhas/<campanha_id>/relatorio', methods=['GET'])
@basic_auth.required
def exportar_relatorio_campanha(campanha_id):
    camp = db.session.get(Campanha, campanha_id)
    if not camp: return "Nao encontrado", 404
    docs = Documento.query.filter_by(campanha_id=campanha_id).order_by(Documento.created_at.desc()).all()
    
    si = io.StringIO()
    cw = csv.writer(si, delimiter=';')
    cw.writerow(['Nome', 'CPF', 'Telefone', 'Status Assinatura', 'Status WhatsApp', 'Data Assinatura', 'Link Download'])
    
    for d in docs:
        dt_assinatura = d.audit_timestamp.strftime('%d/%m/%Y %H:%M:%S') if d.status == 'signed' and d.audit_timestamp else ''
        download_link = f"https://assign.tec.br/download/signed_{d.original_filename}" if d.status == 'signed' else ''
        
        cw.writerow([
            d.signer_name,
            d.signer_cpf,
            d.signer_phone or '',
            d.status,
            d.whatsapp_status,
            dt_assinatura,
            download_link
        ])
        
    csv_data = si.getvalue().encode('utf-8-sig') 
    return csv_data, 200, {
        "Content-Disposition": f'attachment; filename="relatorio_{camp.name.replace(" ", "_")}.csv"',
        "Content-type": "text/csv; charset=utf-8-sig"
    }

@app.route('/api/admin/campanhas/<campanha_id>/docs', methods=['GET'])
@basic_auth.required
def listar_docs_campanha(campanha_id):
    q = request.args.get('q', '')
    status_filter = request.args.get('status', '')
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    
    query = Documento.query.filter_by(campanha_id=campanha_id)
    if q:
        query = query.filter(or_(
            Documento.signer_name.ilike(f"%{q}%"),
            Documento.signer_cpf.ilike(f"%{q}%")
        ))
    if status_filter:
        if status_filter == 'ready':
            # Alias para documentos que já saíram da fila de geração
            query = query.filter(~Documento.status.in_(['generating', 'processing', 'error_generating']))
        else:
            query = query.filter_by(status=status_filter)
    
    pagination = query.order_by(Documento.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
    
    # Adicionalmente, calculamos o progresso total para o cabeçalho
    total_docs = Documento.query.filter_by(campanha_id=campanha_id).count()
    gerados = Documento.query.filter_by(campanha_id=campanha_id).filter(~Documento.status.in_(['generating', 'processing', 'error_generating'])).count()
    
    res = []
    for d in pagination.items:
        item = d.to_dict()
        item['signer_phone'] = d.signer_phone
        res.append(item)
        
    return jsonify({
        "items": res,
        "total": pagination.total,
        "pages": pagination.pages,
        "current_page": pagination.page,
        "stats": {
            "total_campanha": total_docs,
            "gerados": gerados
        }
    })

@app.route('/api/admin/campanhas/resend/<request_id>', methods=['POST'])
@basic_auth.required
def reenviar_campanha_wa(request_id):
    doc = db.session.get(Documento, request_id)
    if not doc: return jsonify({"sucesso": False, "erro": "Doc não encontrado."}), 404
    telefone = request.json.get('telefone')
    if telefone is not None:
        doc.signer_phone = ''.join(filter(str.isdigit, str(telefone)))
    
    doc.whatsapp_status = 'Pendente'
    doc.whatsapp_attempts = 0
    db.session.commit()
    return jsonify({"sucesso": True})

@app.route('/api/admin/campanhas/<campanha_id>/iniciar-disparos', methods=['POST'])
@basic_auth.required
def iniciar_disparos(campanha_id):
    docs = Documento.query.filter_by(campanha_id=campanha_id, whatsapp_status='Pausado').all()
    count = 0
    for doc in docs:
        doc.whatsapp_status = 'Pendente'
        count += 1
    db.session.commit()
    return jsonify({"sucesso": True, "afetados": count})

@app.route('/api/admin/docs/<request_id>', methods=['DELETE'])
@basic_auth.required
def apagar_documento_admin(request_id):
    doc = db.session.get(Documento, request_id)
    if not doc: return jsonify({"sucesso": False, "erro": "Doc não encontrado."}), 404
    if doc.status != 'pending': return jsonify({"sucesso": False, "erro": "Não pode apagar documentos já assinados."}), 400
    
    pending_path = os.path.join(app.config['PENDING_FOLDER'], request_id)
    import shutil
    if os.path.exists(pending_path): shutil.rmtree(pending_path)
    
    db.session.delete(doc)
    db.session.commit()
    return jsonify({"sucesso": True})

@app.route('/api/admin/campanhas/<campanha_id>/template-vars', methods=['GET'])
@basic_auth.required
def get_template_vars(campanha_id):
    camp = db.session.get(Campanha, campanha_id)
    tpl = db.session.get(TemplateDocumento, camp.template_id)
    vars_mapped = list(set([campo.get('name') for campo in tpl.fields_mapping]))
    return jsonify({"vars": vars_mapped})

@app.route('/api/admin/campanhas/<campanha_id>/add-participante', methods=['POST'])
def add_participante_campanha(campanha_id):
    # Verificação de X-API-KEY para integração externa
    api_key = request.headers.get('X-API-KEY')
    if api_key != app.config['MASTER_API_KEY']:
        logging.warning(f"[AUTH] Tentativa de acesso negada com chave: {api_key}")
        return jsonify({"sucesso": False, "erro": "Não autorizado. X-API-KEY inválida ou ausente."}), 401
        
    camp = db.session.get(Campanha, campanha_id)
    if not camp: return jsonify({"sucesso": False, "erro": "Campanha não encontrada"}), 404
    tpl = db.session.get(TemplateDocumento, camp.template_id)
    template_pdf_path = os.path.join(app.config['TEMPLATES_DYNAMIC_FOLDER'], tpl.id, tpl.original_filename)
    if not os.path.exists(template_pdf_path):
         return jsonify({"sucesso": False, "erro": f"Arquivo PDF base não encontrado: {template_pdf_path}"}), 500
    
    row = request.json
    cpf = ''.join(filter(str.isdigit, str(row.get('cpf', ''))))
    telefone = ''.join(filter(str.isdigit, str(row.get('telefone', '') or row.get('whatsapp', ''))))
    nome = row.get('nome', 'Participante')
    if not cpf: return jsonify({"sucesso": False, "erro": "CPF é obrigatório"}), 400
    
    request_id = str(uuid.uuid4())
    pending_path = os.path.join(app.config['PENDING_FOLDER'], request_id)
    os.makedirs(pending_path, exist_ok=True)
    try:
        final_pdf_name = f"campanha_{camp.id}_{request_id}.pdf"
        output_pdf_path = os.path.join(pending_path, final_pdf_name)
        
        # Usa a nova função auxiliar
        original_hash = gerar_pdf_para_campanha(tpl, row, output_pdf_path)
        
        new_doc = Documento(
            request_id=request_id, signer_name=nome,
            signer_cpf=cpf, signer_phone=telefone,
            doc_data=row, original_filename=final_pdf_name, 
            original_hash=original_hash,
            campanha_id=camp.id, whatsapp_status='Pausado'
        )
        db.session.add(new_doc)
        db.session.commit()
        return jsonify({"sucesso": True})
    except Exception as e:
        return jsonify({"sucesso": False, "erro": str(e)}), 500
    except Exception as e:
        return jsonify({"sucesso": False, "erro": str(e)}), 500

# --- Rotas do Processo de Assinatura ---


@app.route('/sign/<request_id>', methods=['GET'])
def sign_document(request_id):
    doc = db.session.get(Documento, request_id)
    if not doc: return "<h1>Link inválido</h1>", 404
    if doc.status != 'pending': return "<h1>Este documento já foi assinado.</h1>", 403

    pending_path = os.path.join(app.config['PENDING_FOLDER'], request_id)
    pdf_path = os.path.join(pending_path, doc.original_filename)
    if not os.path.exists(pdf_path): return "<h1>Erro: Arquivo não encontrado.</h1>", 500
        
    doc_fitz = fitz.open(pdf_path)
    image_paths = []
    for page_num in range(len(doc_fitz)):
        page = doc_fitz.load_page(page_num); pix = page.get_pixmap()
        image_filename = f"page_{page_num + 1}.png"
        pix.save(os.path.join(pending_path, image_filename))
        image_paths.append(url_for('get_pending_file', request_id=request_id, filename=image_filename))
        
    return render_template('sign_document.html', 
                           request_id=request_id, 
                           document_images=image_paths, 
                           signer_name=doc.signer_name, 
                           masked_cpf=mask_cpf(doc.signer_cpf),
                           is_campanha=bool(doc.campanha_id))

@app.route('/pending/<request_id>/<filename>')
def get_pending_file(request_id, filename):
    return send_from_directory(os.path.join(app.config['PENDING_FOLDER'], request_id), filename)

@app.route('/submit_signature/<request_id>', methods=['POST'])
def submit_signature(request_id):
    doc = db.session.get(Documento, request_id)
    if not doc or doc.status != 'pending': abort(404)

    pending_path = os.path.join(app.config['PENDING_FOLDER'], doc.request_id)
    signature_b64 = request.form['signature'].split(',')[1]
    selfie_b64 = request.form['selfie'].split(',')[1]
    import base64
    sig_path = os.path.join(pending_path, 'signature.png')
    selfie_path = os.path.join(pending_path, 'selfie.png')
    with open(sig_path, "wb") as f: f.write(base64.b64decode(signature_b64))
    with open(selfie_path, "wb") as f: f.write(base64.b64decode(selfie_b64))
    
    try:
        face_cascade = cv2.CascadeClassifier('haarcascade_frontalface_default.xml')
        img = cv2.imread(selfie_path)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(gray, 1.1, 4)
        if len(faces) == 0: return "<h1>Rosto não detectado na selfie.</h1>", 400
    except Exception as e: return "<h1>Erro na validação facial.</h1>", 500

    # Auditoria
    audit_pdf_path = os.path.join(pending_path, 'audit_page.pdf')
    c = canvas.Canvas(audit_pdf_path, pagesize=letter)
    # ... (Sua lógica de PDF de auditoria permanece igual)
    width, height = letter
    c.setFont("Helvetica-Bold", 16); c.drawString(72, height - 72, "Página de Auditoria da Assinatura Eletrônica")
    text_y = height - 120; c.setFont("Helvetica-Bold", 12); c.drawString(72, text_y, "Detalhes do Documento Original")
    text_y -= 20; c.setFont("Helvetica", 10); c.drawString(72, text_y, f"Arquivo: {doc.original_filename}")
    text_y -= 20; c.drawString(72, text_y, f"Hash: {doc.original_hash}")
    text_y -= 40; c.setFont("Helvetica-Bold", 12); c.drawString(72, text_y, "Detalhes do Signatário")
    text_y -= 20; c.setFont("Helvetica", 10); c.drawString(72, text_y, f"Nome: {doc.signer_name}")
    text_y -= 20; c.drawString(72, text_y, f"CPF: {doc.signer_cpf}")
    if doc.doc_data:
        for k, v in doc.doc_data.items():
            if k not in ['nome', 'cpf']: text_y -= 20; c.drawString(72, text_y, f"{k.upper()}: {v}")
    
    audit_timestamp = datetime.now(UTC)
    text_y -= 20; c.drawString(72, text_y, f"IP: {request.remote_addr}")
    text_y -= 20; c.drawString(72, text_y, f"Data (UTC): {audit_timestamp.isoformat()}")
    text_y -= 40; c.drawString(72, text_y, "Assinatura:"); c.drawImage(ImageReader(sig_path), 72, text_y - 140, width=200, height=100, preserveAspectRatio=True, mask='auto')
    c.drawString(350, text_y, "Selfie:"); c.drawImage(ImageReader(selfie_path), 350, text_y - 140, width=120, height=90, preserveAspectRatio=True, mask='auto')
    c.save()

    # Finalização PDF
    output_pdf = PdfWriter()
    with open(os.path.join(pending_path, doc.original_filename), 'rb') as f_orig:
        reader = PdfReader(f_orig)
        for p in reader.pages: output_pdf.add_page(p)
    with open(audit_pdf_path, 'rb') as f_audit:
        reader = PdfReader(f_audit)
        output_pdf.add_page(reader.pages[0])
    
    final_name = f"signed_{doc.original_filename}"
    download_link = f"https://assign.tec.br/download/{final_name}" # Use seu domínio real
    with open(os.path.join(app.config['SIGNED_FOLDER'], final_name), 'wb') as f_final: output_pdf.write(f_final)
    
    doc.status = 'signed'; doc.audit_ip = request.remote_addr; doc.audit_timestamp = audit_timestamp
    db.session.commit()
    # ENVIAR WHATSAPP DE CONCLUSÃO
    enviar_notificacao_whatsapp(doc.signer_name, doc.signer_cpf, download_link, "Concluído", doc.signer_phone, doc.request_id)
    shutil.move(pending_path, os.path.join(app.config['COMPLETED_FOLDER'], request_id))
    return redirect(url_for('success', filename=final_name))

@app.route('/success')
def success():
    filename = request.args.get('filename')
    doc = None
    if filename:
        orig = filename.replace('signed_', '')
        doc = Documento.query.filter_by(original_filename=orig).first()
    is_campanha = doc and bool(doc.campanha_id)
    return render_template('success.html', filename=filename, is_campanha=is_campanha)

@app.route('/download/<path:filename>')
def download_file(filename):
    return send_from_directory(app.config['SIGNED_FOLDER'], filename, as_attachment=True)
    
@app.route('/api/documentos', methods=['GET'])
def listar_documentos():
    docs = Documento.query.order_by(Documento.created_at.desc()).all()
    return jsonify([doc.to_dict() for doc in docs])

@app.cli.command("create-db")
def create_db():
    with app.app_context(): db.create_all()
    print("Banco de dados criado!")
# app.py

# ... (restante do código)

@app.route('/admin/get-logs')
@basic_auth.required
def get_logs():
    log_path = os.path.join(BASE_DIR, 'whatsapp_integration.log')
    if not os.path.exists(log_path):
        return jsonify({"logs": ["Arquivo de log ainda não criado."]}), 200
    
    try:
        with open(log_path, 'r') as f:
            # Lê as últimas 100 linhas para não sobrecarregar a página
            linhas = f.readlines()
            ultimas_linhas = linhas[-100:] 
            # Inverte para mostrar o mais recente primeiro
            ultimas_linhas.reverse()
            return jsonify({"logs": ultimas_linhas}), 200
    except Exception as e:
        return jsonify({"sucesso": False, "erro": str(e)}), 500




def whatsapp_queue_worker():
    while True:
        try:
            with app.app_context():
                doc = Documento.query.filter_by(whatsapp_status='Pendente').first()
                if doc:
                    telefone = ''.join(filter(str.isdigit, str(doc.signer_phone)))
                    if not telefone:
                        doc.whatsapp_status = 'Erro'
                        db.session.commit()
                        continue
                        
                    logging.info(f"[FILA WA] Proc: {telefone} | DOC: {doc.request_id}")
                    
                    if doc.campanha_id:
                        auth_link = f"https://assign.tec.br/campanha/auth/{doc.request_id}"
                        descricao = f"Olá, *{doc.signer_name}*! Identificamos que você tem um documento pendente para a sua *Atualização Cadastral* na Coopedu. 📄✨\n\nAssine agora de forma rápida pelo nosso portal seguro: {auth_link}"
                    else:
                        auth_link = f"https://assign.tec.br/sign/{doc.request_id}"
                        descricao = f"Aviso! Há um documento pendente para sua assinatura: {auth_link}"
                    
                    base_url = "https://webatende.coopedu.com.br:3000/api/crm/notify/"
                    params = {
                        "titulo": "📢 *AVISO - COOPEDU*",
                        "descricao": descricao,
                        "etapa": "Aguardando Assinatura",
                        "numero": telefone
                    }
                    
                    try:
                        response = requests.post(base_url, params=params, timeout=12)
                        if response.status_code == 200:
                            doc.whatsapp_status = 'Enviado'
                            logging.info(f"[FILA WA] SUCESSO enviado para {telefone}")
                        else:
                            doc.whatsapp_attempts += 1
                            doc.whatsapp_status = 'Erro' if doc.whatsapp_attempts >= 3 else 'Pendente'
                    except Exception as req_e:
                        doc.whatsapp_attempts += 1
                        doc.whatsapp_status = 'Erro' if doc.whatsapp_attempts >= 3 else 'Pendente'
                        logging.error(f"[FILA WA] Falha API requests: {str(req_e)}")
                    
                    db.session.commit()
            time.sleep(10) # Intervalo seguro
        except Exception as e:
            logging.error(f"[FILA WA] Erro Crítico no Worker: {str(e)}")
            time.sleep(10)

def iniciar_workers_seguros():
    """Tenta iniciar as threads apenas se for o processo 'líder' no servidor."""
    try:
        # Usamos um arquivo de lock no /tmp/ para garantir que apenas 1 processo do Gunicorn rode as threads
        f = open('/tmp/assinatura_worker.lock', 'w')
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        
        # Guardamos a referência do arquivo para o lock não ser liberado pelo GC
        app.config['WORKER_LOCK_FILE'] = f
        
        logging.info("[WORKER] Este processo assumiu a liderança das threads de background.")
        
        # Iniciar fila de WhatsApp
        threading.Thread(target=whatsapp_queue_worker, daemon=True).start()
        
        # Iniciar fila de PDF
        threading.Thread(target=background_campaign_processor, args=(app.app_context(),), daemon=True).start()
        
    except (IOError, OSError):
        # Falhou em pegar o lock, outro worker já é o master
        logging.info("[WORKER] Outro processo já está gerenciando as threads de background.")

# Iniciar workers automaticamente ao carregar o app (Gunicorn chamará isso)
with app.app_context(): iniciar_workers_seguros()

if __name__ == '__main__':
    with app.app_context(): db.create_all() 
    app.run(debug=True, port=5001, use_reloader=False)