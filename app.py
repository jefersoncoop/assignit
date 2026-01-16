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
from flask_cors import CORS 
from flask_basicauth import BasicAuth 
from werkzeug.utils import secure_filename
import urllib.parse # NOVA IMPORTAÇÃO
import requests

import fitz
from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from PyPDF2 import PdfWriter, PdfReader

# --- Configuração do App e Pastas ---
app = Flask(__name__)
CORS(app) 
app.config['BASIC_AUTH_USERNAME'] = 'admin'
app.config['BASIC_AUTH_PASSWORD'] = 'admin123'
basic_auth = BasicAuth(app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'assinaturas.db')
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{DB_PATH}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

app.config['PENDING_FOLDER'] = os.path.join(BASE_DIR, 'pending')
app.config['SIGNED_FOLDER'] = os.path.join(BASE_DIR, 'signed')
app.config['COMPLETED_FOLDER'] = os.path.join(BASE_DIR, 'completed')
app.config['TEMPLATES_PDF_FOLDER'] = os.path.join(BASE_DIR, 'templates_pdf')

for folder_key in ['PENDING_FOLDER', 'SIGNED_FOLDER', 'COMPLETED_FOLDER', 'TEMPLATES_PDF_FOLDER']:
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

    def to_dict(self):
        return {
            "request_id": self.request_id,
            "status": self.status,
            "nome_arquivo": self.original_filename,
            "nome_signatario": self.signer_name,
            "cpf_signatario": self.signer_cpf,
            "data_criacao": self.created_at.isoformat() if self.created_at else None,
            "data_assinatura": self.audit_timestamp.isoformat() if self.audit_timestamp else None
        }

# --- Funções Auxiliares ---
def calculate_hash(filepath):
    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def enviar_notificacao_whatsapp(nome, cpf, link, etapa, numero):
    try:
        # Limpa o número (deixa apenas dígitos)
        telefone = ''.join(filter(str.isdigit, str(numero)))
        
        # Formata a descrição conforme seu modelo
        descricao = f"Solicitação de desligamento recebida {nome} \nCPF: {cpf}\nLink: {link}"
        if etapa == "Concluído":
            descricao = f"Assinatura Concluída! {nome} \nSeu documento já está disponível.\nDownload: {link}"

        # Monta a URL de destino
        base_url = "https://webatende.coopedu.com.br:3000/api/crm/notify/"
        params = {
            "titulo": "📢 *AVISO - COOPEDU*",
            "descricao": descricao,
            "etapa": etapa,
            "numero": telefone
        }
        
        # Envia o POST
        response = requests.post(base_url, params=params, timeout=10)
        return response.status_code == 200
    except Exception as e:
        print(f"Erro ao enviar WhatsApp: {e}")
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
    try:
        docs_objects = Documento.query.order_by(Documento.created_at.desc()).all()
        all_docs = [doc.to_dict() for doc in docs_objects]
    except Exception as e:
        print(f"Erro ao buscar documentos: {e}")
        all_docs = []
    return render_template('admin.html', all_docs_json=json.dumps(all_docs))

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
            "signing_link": url_for('sign_document', request_id=existente.request_id, _external=True)
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
                           masked_cpf=mask_cpf(doc.signer_cpf))

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
    enviar_notificacao_whatsapp(doc.signer_name, doc.signer_cpf, download_link, "Concluído", doc.signer_phone)
    shutil.move(pending_path, os.path.join(app.config['COMPLETED_FOLDER'], request_id))
    return redirect(url_for('success', filename=final_name))

@app.route('/success')
def success():
    return render_template('success.html', filename=request.args.get('filename'))

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

if __name__ == '__main__':
    with app.app_context(): db.create_all() 
    app.run(debug=True, port=5001)