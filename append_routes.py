import re

with open('app.py', 'r') as f:
    content = f.read()

new_routes = """
# --- ROTAS DE CAMPANHA ---
@app.route('/api/admin/campanhas', methods=['GET'])
@basic_auth.required
def listar_campanhas():
    campanhas = Campanha.query.order_by(Campanha.created_at.desc()).all()
    res = []
    for c in campanhas:
        docs = Documento.query.filter_by(campanha_id=c.id).count()
        d = c.to_dict()
        d['total_docs'] = docs
        res.append(d)
    return jsonify(res)

@app.route('/api/admin/campanhas/upload', methods=['POST'])
@basic_auth.required
def upload_campanhas_csv():
    nome = request.form.get('nome')
    template_id = request.form.get('template_id')
    if 'csv_file' not in request.files or not template_id or not nome:
        return jsonify({"sucesso": False, "erro": "Dados incompletos"}), 400
        
    tpl = db.session.get(TemplateDocumento, template_id)
    if not tpl: return jsonify({"sucesso": False, "erro": "Template não encontrado."}), 404
        
    template_pdf_path = os.path.join(app.config['TEMPLATES_DYNAMIC_FOLDER'], tpl.id, tpl.original_filename)
    if not os.path.exists(template_pdf_path):
        return jsonify({"sucesso": False, "erro": "Arquivo PDF base do template ausente."}), 500

    new_campanha = Campanha(name=nome, template_id=template_id)
    db.session.add(new_campanha)
    
    file = request.files['csv_file']
    stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
    csv_input = csv.DictReader(stream)
    
    count = 0
    for row in csv_input:
        cpf_key = next((k for k in row.keys() if k.strip().lower() == 'cpf'), None)
        if not cpf_key: continue
        
        cpf = row[cpf_key]
        telefone = row.get('telefone', '')
        nome_linha = row.get('nome', 'Participante')
        
        if not cpf: continue
        
        request_id = str(uuid.uuid4())
        pending_path = os.path.join(app.config['PENDING_FOLDER'], request_id)
        os.makedirs(pending_path, exist_ok=True)
        final_pdf_name = f"campanha_{new_campanha.id}_{request_id}.pdf"
        output_pdf_path = os.path.join(pending_path, final_pdf_name)
        
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
                        val = row.get(var_name, '')
                        if val is None: val = ''
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
            original_hash = calculate_hash(output_pdf_path)
            
            new_doc = Documento(
                request_id=request_id, signer_name=nome_linha,
                signer_cpf=cpf, signer_phone=telefone,
                doc_data=row, original_filename=final_pdf_name, 
                original_hash=original_hash,
                campanha_id=new_campanha.id, whatsapp_status='Pendente'
            )
            db.session.add(new_doc)
            count += 1
            
        except Exception as e:
            logging.error(f"[CAMPANHA ERRO] Linha ignorada: {str(e)}")
            continue

    db.session.commit()
    return jsonify({"sucesso": True, "campanha_id": new_campanha.id, "total": count})

@app.route('/campanha/auth/<request_id>', methods=['GET'])
def campanha_auth(request_id):
    doc = db.session.get(Documento, request_id)
    if not doc or not doc.campanha_id: return "<h1>Inválido</h1>", 404
    if doc.status == 'signed': return redirect(url_for('success', filename=f"signed_{doc.original_filename}"))
    return render_template('campanha_auth.html', request_id=request_id)

@app.route('/api/campanha/auth/validar', methods=['POST'])
def validate_campanha_auth():
    dados = request.json
    request_id = dados.get('request_id')
    cpf = dados.get('cpf')
    
    doc = db.session.get(Documento, request_id)
    if not doc or not doc.campanha_id: return jsonify({"sucesso": False, "erro": "Documento inválido"}), 404
    
    cpf_limpo = ''.join(filter(str.isdigit, cpf))
    doc_cpf_limpo = ''.join(filter(str.isdigit, doc.signer_cpf))
    
    if cpf_limpo != doc_cpf_limpo:
        return jsonify({"sucesso": False, "erro": "CPF incorreto."}), 403
        
    return jsonify({"sucesso": True})

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

# --- Rotas do Processo de Assinatura ---
"""

content = content.replace("# --- Rotas do Processo de Assinatura ---", new_routes)

with open('app.py', 'w') as f:
    f.write(content)
