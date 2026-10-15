"""ZX AI Commerce Studio MVP. Start: python -m uvicorn app:app --host 127.0.0.1 --port 8765"""
from __future__ import annotations
import csv, io, json, os, re, shutil, threading, time, uuid, zipfile, hmac, hashlib, base64, secrets
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Form
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from PIL import Image, ImageOps, ImageFile
from engine import recognize_product, demo_background, ai_background, isolate_product, compose_image, fallback_details

ROOT=Path(__file__).resolve().parent
DATA=Path(os.getenv('ZX_DATA_DIR', str(ROOT/'data'))).resolve();DATA.mkdir(parents=True,exist_ok=True)
ASSETS=ROOT/'assets';STATIC=ROOT/'static'
MAX_FILES=300;MAX_IMAGE_BYTES=25*1024*1024;MAX_ARCHIVE_BYTES=250*1024*1024;MAX_TOTAL_UNPACKED=350*1024*1024
CLOUD=os.getenv("ZX_CLOUD_MODE","0")=="1"
ACCESS_PASSWORD=os.getenv("ZX_ACCESS_PASSWORD", "")
if CLOUD and len(ACCESS_PASSWORD)<12:
    raise RuntimeError("Cloud deployment requires ZX_ACCESS_PASSWORD with at least 12 characters")
AUTH_SECRET=hashlib.pbkdf2_hmac("sha256", ACCESS_PASSWORD.encode(), b"ZX_COMMERCE_OWNER_SESSION_v1", 130000) if CLOUD else b""
MAX_BATCH_AI=int(os.getenv("ZX_MAX_AI_IMAGES_PER_JOB", "30"))
MAX_UPLOAD_BYTES=int(os.getenv("ZX_MAX_UPLOAD_MB", "160"))*1024*1024
LOGIN_ATTEMPTS={};LOGIN_LOCK=threading.RLock()
Image.MAX_IMAGE_PIXELS=42_000_000
JOBS={};LOCK=threading.RLock();POOL=ThreadPoolExecutor(max_workers=1 if CLOUD else 2)

@asynccontextmanager
async def lifespan(app):
    for p in DATA.iterdir():
        if p.is_dir() and (p/'job.json').exists():
            try:
                job=json.loads((p/'job.json').read_text('utf-8'))
                if job['status'] in ('running','queued'):
                    job['status']='interrupted'
                    for item in job['items']:
                        if item['status'] in ('running','queued'):item['status']='waiting'
                JOBS[job['id']]=job
            except Exception: pass
    yield
    POOL.shutdown(wait=False, cancel_futures=False)
app=FastAPI(title='ZX AI Commerce Studio', version='0.1.0', lifespan=lifespan)
app.mount('/static', StaticFiles(directory=str(STATIC)), name='static')
app.mount('/assets',StaticFiles(directory=str(ASSETS)),name='assets')

def _session_token():
    when=str(int(time.time()))
    nonce=secrets.token_hex(12)
    data=when+'.'+nonce
    sig=hmac.new(AUTH_SECRET,data.encode(),hashlib.sha256).hexdigest()
    return data+'.'+sig

def _session_valid(token):
    if not token or len(token)>200:return False
    try:
        when,nonce,sig=token.split('.')
        when_n=int(when)
        if not (0 <=time.time()-when_n<7*24*3600):return False
        if len(nonce)!=24 or not re.fullmatch(r'[a-f0-9]{24}',nonce):return False
        data=when+'.'+nonce
        expected=hmac.new(AUTH_SECRET,data.encode(),hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig,expected)
    except (ValueError,TypeError):return False

@app.middleware('http')
async def cloud_access_guard(request:Request,call_next):
    path=request.url.path
    if CLOUD and path not in ('/login','/api/auth/login','/api/healthz') and not path.startswith('/static/'):
        if not _session_valid(request.cookies.get('zx_owner_session')):
            if path=='/':return RedirectResponse('/login',status_code=303)
            return JSONResponse({'detail':'请先登录工作台'},status_code=401)
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='same-origin'
    response.headers['X-Frame-Options']='DENY'
    response.headers['Content-Security-Policy']="default-src 'self'; img-src 'self' blob: data:; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'"
    if CLOUD and (path.startswith('/api/') or path=='/login'):
        response.headers['Cache-Control']='no-store'
    return response

@app.get('/api/healthz')
def healthz():return {'status':'ok'}

@app.get('/login')
def login_screen(request:Request):
    if not CLOUD:return RedirectResponse('/',status_code=303)
    if _session_valid(request.cookies.get('zx_owner_session')):
        return RedirectResponse('/',status_code=303)
    return FileResponse(STATIC/'login.html')

class LoginBody(BaseModel):
    password:str=Field(min_length=1,max_length=500)

@app.post('/api/auth/login')
def login(request:Request,data:LoginBody):
    if not CLOUD:return {'ok':True}
    ip=request.client.host if request.client else 'unknown'
    now=time.time()
    with LOGIN_LOCK:
        recent=[t for t in LOGIN_ATTEMPTS.get(ip,[]) if now-t<900]
        if len(recent)>=8:raise HTTPException(429,'尝试次数过多，请15分钟后再试')
        if not hmac.compare_digest(data.password,ACCESS_PASSWORD):
            recent.append(now);LOGIN_ATTEMPTS[ip]=recent
            raise HTTPException(401,'访问密码错误')
        LOGIN_ATTEMPTS.pop(ip,None)
    resp=JSONResponse({'ok':True})
    resp.set_cookie('zx_owner_session',_session_token(),max_age=7*24*3600,secure=True,httponly=True,samesite='lax',path='/')
    return resp

@app.post('/api/auth/logout')
def logout():
    resp=JSONResponse({'ok':True})
    resp.delete_cookie('zx_owner_session',path='/')
    return resp


def _save(job):
    (DATA/job['id']/'job.json').write_text(json.dumps(job,ensure_ascii=False,indent=2),encoding='utf-8')
def _change(job, item=None, **kwargs):
    with LOCK:
        (item if item is not None else job).update(kwargs)
        job['updated']=time.time();_save(job)
def _job(jobid):
    with LOCK:
        if jobid not in JOBS:raise HTTPException(404,'任务不存在')
        return JOBS[jobid]
def _safe_filename(name):
    name=name.replace('\\','/').split('/')[-1].strip()[:120]
    name=re.sub(r'[\x00-\x1f<>:"|?*]','_',name).strip(' .')
    return name or 'unnamed.png'
def _supported(name):return Path(name).suffix.lower() in {'.png','.jpg','.jpeg','.webp','.bmp'}
def _validated_bytes(data, name):
    if len(data)>MAX_IMAGE_BYTES:raise ValueError('单张图片超过25MB')
    try:
        im=Image.open(io.BytesIO(data));im.verify()
        im=Image.open(io.BytesIO(data))
        if im.width*im.height>42_000_000:raise ValueError('图片像素过大')
        if im.width<80 or im.height<80:raise ValueError('图片过小')
        rgb=ImageOps.exif_transpose(im).convert('RGBA' if 'A' in im.getbands() else 'RGB')
        out=io.BytesIO();rgb.save(out,'PNG')
        return out.getvalue()
    except Exception as e:raise ValueError(f'{name}：不是有效图片：{e}')

def _add_image(job, raw, name):
    if len(job['items'])>=MAX_FILES:raise ValueError(f'一次最多导入{MAX_FILES}张图片')
    safe=_safe_filename(name)
    if not _supported(safe):return
    data=_validated_bytes(raw,safe)
    itemid=f'{len(job["items"])+1:04d}'
    dst=DATA/job['id']/'source'/f'{itemid}.png';dst.write_bytes(data)
    job['items'].append({'id':itemid,'name':safe,'status':'waiting','stage':'等待制作','source':f'source/{itemid}.png','image':None,'background':None,'cutout':None,'details':None,'warnings':[],'error':None})

def _load_csv(job, text):
    rows=csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
    aliases={'filename':['文件名','商品图','filename','file','name'],'main':['主卖点','主标题','main','headline'],'subtitle':['副卖点','副标题','subtitle','sub'],'theme':['文字颜色','theme','color']}
    cmap={key:next((k for k in a if k in (rows.fieldnames or [])),None) for key,a in aliases.items()}
    if not cmap['filename']:raise ValueError('CSV缺少「文件名」列')
    cnt=0
    for row in rows:
        base=_safe_filename((row.get(cmap['filename']) or ''))
        stem=Path(base).stem.lower()
        target=next((i for i in job['items'] if i['name'].lower()==base.lower()),None)
        if target is None:target=next((i for i in job['items'] if Path(i['name']).stem.lower()==stem),None)
        if not target:continue
        target.setdefault('overrides',{})
        for k in ['main','subtitle','theme']:
            if cmap[k] and row.get(cmap[k]):target['overrides'][k]=row[cmap[k]].replace('\\n','\n').strip()
        cnt+=1
    _save(job);return cnt

@app.get('/')
def index():return FileResponse(STATIC/'index.html')
@app.get('/api/health')
def health():return {'status':'ok','ai_available':bool(os.getenv('OPENAI_API_KEY')),'cutout_rembg':_has_rembg(),'max_files':MAX_FILES,'image_model':os.getenv('OPENAI_IMAGE_MODEL','gpt-image-1.5')}
def _has_rembg():
    import importlib.util
    return importlib.util.find_spec('rembg') is not None

@app.post('/api/jobs')
async def create_job(files:list[UploadFile]=File(...)):
    jid=uuid.uuid4().hex[:12];path=DATA/jid
    for d in ['source','background','cutout','output']:(path/d).mkdir(parents=True,exist_ok=True)
    job={'id':jid,'created':time.time(),'updated':time.time(),'status':'uploaded','mode':None,'settings':{},'items':[],'warning_messages':[],'logo':'assets/default-logo.png'}
    try:
        total_uploaded=0
        for f in files:
            contents=await f.read(min(MAX_ARCHIVE_BYTES,MAX_UPLOAD_BYTES-total_uploaded)+1)
            total_uploaded+=len(contents)
            if total_uploaded>MAX_UPLOAD_BYTES:raise ValueError(f"一次上传数据最多{MAX_UPLOAD_BYTES//1024//1024}MB")
            name=f.filename or 'unnamed'; ext=Path(name).suffix.lower()
            if ext=='.zip':
                if len(contents)>MAX_ARCHIVE_BYTES:raise ValueError('ZIP超过250MB')
                with zipfile.ZipFile(io.BytesIO(contents)) as z:
                    total=0
                    for info in z.infolist():
                        if info.is_dir() or not _supported(info.filename):continue
                        if (info.external_attr>>16)&0o170000==0o120000:continue
                        total+=info.file_size
                        if info.file_size>MAX_IMAGE_BYTES or total>MAX_TOTAL_UNPACKED:raise ValueError('ZIP解压内容超出大小限制')
                        with z.open(info) as fp:_add_image(job,fp.read(MAX_IMAGE_BYTES+1),info.filename)
            elif _supported(name):_add_image(job,contents,name)
        if not job['items']:raise ValueError('没有检测到有效的商品图片')
    except Exception as e:
        shutil.rmtree(path,ignore_errors=True)
        raise HTTPException(400,str(e))
    with LOCK:JOBS[jid]=job;_save(job)
    return _public(job)

@app.get('/api/jobs')
def list_jobs():
    with LOCK:return [{'id':x['id'],'created':x['created'],'status':x['status'],'count':len(x['items'])} for x in sorted(JOBS.values(),key=lambda x:x['created'],reverse=True)[:25]]

@app.get('/api/jobs/{jid}')
def get_job(jid:str):return _public(_job(jid))

def _public(job):
    with LOCK:
        obj=json.loads(json.dumps(job))
    for item in obj['items']:
        item['source_url']=f'/data/{obj["id"]}/{item["source"]}'
        item['output_url']=f'/data/{obj["id"]}/{item["image"]}?v={int(obj["updated"]*1000)}' if item['image'] else None
    obj['logo_url']='/'+obj['logo'] if obj['logo'].startswith('assets/') else (f'/data/{obj["id"]}/{obj["logo"]}' if obj['logo'] else None)
    return obj

@app.get('/data/{jid}/{path:path}')
def data_file(jid:str,path:str):
    _job(jid)
    base=(DATA/jid).resolve()
    target=(base/path).resolve()
    if not target.is_relative_to(base) or not target.is_file():raise HTTPException(404)
    return FileResponse(target)

class Start(BaseModel):
    mode:str='demo'
    size:int=1125
    headline_px:int=70
    subtitle_px:int=26
    with_logo:bool=True

@app.post('/api/jobs/{jid}/start')
def start_job(jid:str, options:Start):
    job=_job(jid)
    if options.mode not in ('demo','ai'):raise HTTPException(400,'mode必须为demo或ai')
    if options.size not in (800,1125,1200):raise HTTPException(400,'输出尺寸不支持')
    if not (40<=options.headline_px<=90 and 16<=options.subtitle_px<=40):raise HTTPException(400,'文字字号不正确')
    if options.mode=='ai' and not os.getenv('OPENAI_API_KEY'):raise HTTPException(400,'服务器尚未配置OPENAI_API_KEY，请先使用演示模式')
    if options.mode=='ai' and len(job['items'])>MAX_BATCH_AI:raise HTTPException(400,f'当前AI批量额度最多{MAX_BATCH_AI}张，请分批上传或联系管理员调整')
    with LOCK:
        if job['status'] in ('running','queued'):raise HTTPException(409,'任务正在运行')
        settings=options.model_dump();job.update(mode=options.mode,settings=settings,status='queued')
        for item in job['items']:
            # A second full run intentionally rebuilds every item with the latest parameters.
            item.update(status='queued',error=None,stage='排队中')
        _save(job)
    POOL.submit(_process_job,job)
    return _public(job)

def _work_item(job,item,redo_bg=False):
    jid=job['id'];base=DATA/jid;mode=job['mode'];cfg=job['settings'];src=base/item['source']
    def stage(t):_change(job,item,status='running',stage=t,error=None)
    stage('识别商品 / 生成卖点')
    if item.get('details') and redo_bg:
        details=item['details']
    else:
        details=recognize_product(src,os.getenv('OPENAI_API_KEY') if mode=='ai' else None,item['name'])
        details.update(item.get('overrides',{}))
        _change(job,item,details=details)
    stage('生成场景背景')
    bgp=base/'background'/f'{item["id"]}.png'
    if mode=='ai':
        ai_background(cfg['size'],details,bgp,os.getenv('OPENAI_API_KEY'),os.getenv('OPENAI_IMAGE_MODEL','gpt-image-1.5'),os.getenv('OPENAI_IMAGE_QUALITY','medium'))
    else:demo_background(cfg['size'],details['category'],int(item['id'])).save(bgp)
    _change(job,item,background=f'background/{item["id"]}.png')
    stage('商品抠图 / 保留包装')
    cp=base/'cutout'/f'{item["id"]}.png'
    cut,warn=isolate_product(src,cp)
    _change(job,item,cutout=(f'cutout/{item["id"]}.png' if cut is not None else None))
    stage('合成文案 / 精确排版')
    out=base/'output'/f'{item["id"]}_{Path(item["name"]).stem[:55]}.png'
    logo=(ASSETS/'default-logo.png' if job['logo'].startswith('assets/') else base/job['logo']) if cfg.get('with_logo',True) else None
    warnings=compose_image(bgp,cp if cut is not None else None,out,details,cfg,logo)
    if warn:warnings.insert(0,warn)
    if details.get('copy_source'):warnings.append(details['copy_source'])
    _change(job,item,status='done',stage='已完成（建议审核）',image=str(out.relative_to(base)),warnings=warnings)

def _process_job(job, ids=None, redo_bg=False):
    _change(job,status='running')
    selected=([x for x in job['items'] if x['id'] in ids] if ids else [x for x in job['items'] if x['status'] in ('queued','waiting','error')])
    for item in selected:
        try:_work_item(job,item,redo_bg)
        except Exception as exc:
            _change(job,item,status='error',stage='失败',error=str(exc)[:600])
    _change(job,status='finished' if all(i['status']=='done' for i in job['items']) else 'partial')

class Edit(BaseModel):
    main:str=Field(min_length=1,max_length=70)
    subtitle:str=Field(min_length=1,max_length=120)
    theme:str='#2f4e37'

@app.post('/api/jobs/{jid}/items/{itemid}/edit')
def edit(jid:str,itemid:str,data:Edit):
    job=_job(jid); item=next((i for i in job['items'] if i['id']==itemid),None)
    if item is None:raise HTTPException(404,'商品不存在')
    if item['status']=='running':raise HTTPException(409,'当前商品正在制作')
    with LOCK:
        item.setdefault('overrides',{}).update(main=data.main,subtitle=data.subtitle,theme=data.theme)
        if item.get('details'):
            item['details'].update(item['overrides'])
        _save(job)
    if item.get('background') and item.get('image') and item.get('details'):
        base=DATA/jid
        logo=(ASSETS/'default-logo.png' if job['logo'].startswith('assets/') else base/job['logo']) if job['settings'].get('with_logo',True) else None
        warnings=compose_image(base/item['background'],base/item['cutout'] if item['cutout'] else None,base/item['image'],item['details'],job['settings'],logo)
        _change(job,item,warnings=warnings+[item['details'].get('copy_source','人工编辑内容')])
    return _public(job)

@app.post('/api/jobs/{jid}/items/{itemid}/regenerate')
def regenerate(jid:str,itemid:str):
    job=_job(jid);item=next((i for i in job['items'] if i['id']==itemid),None)
    if not item:raise HTTPException(404)
    if job['status'] in ('running','queued'):raise HTTPException(409,'任务正在运行')
    with LOCK:item['status']='queued';item['stage']='准备重做背景';job['status']='queued';_save(job)
    POOL.submit(_process_job,job,{itemid},True)
    return _public(job)

@app.post('/api/jobs/{jid}/logo')
async def upload_logo(jid:str,file:UploadFile=File(...)):
    job=_job(jid);raw=await file.read(15*1024*1024)
    try:
        im=Image.open(io.BytesIO(raw)).convert('RGBA');im.thumbnail((1100,1100))
        target=DATA/jid/'logo.png';im.save(target)
    except Exception:raise HTTPException(400,'LOGO图片无效')
    _change(job,logo='logo.png')
    return _public(job)

@app.post('/api/jobs/{jid}/csv')
async def csv_overrides(jid:str,file:UploadFile=File(...)):
    job=_job(jid);raw=await file.read(2*1024*1024)
    try:
        try:text=raw.decode('utf-8-sig')
        except UnicodeDecodeError:text=raw.decode('gbk')
        count=_load_csv(job,text)
        return {'updated':count}
    except Exception as e:raise HTTPException(400,str(e))

@app.get('/api/jobs/{jid}/download')
def download_zip(jid:str):
    job=_job(jid);base=DATA/jid
    if not any(i.get('image') for i in job['items']):raise HTTPException(400,'暂无可导出图片')
    dest=base/'ZX_电商主图_批量导出.zip'
    with zipfile.ZipFile(dest,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=5) as z:
        for item in job['items']:
            if item['image']:
                filename=f'{item["id"]}_{_safe_filename(Path(item["name"]).stem)}.png'
                z.write(base/item['image'],f'主图/{filename}')
        b=io.StringIO();csvw=csv.writer(b)
        csvw.writerow(['文件名','产品识别','主卖点','副卖点','文字颜色','状态','审核提示'])
        for i in job['items']:
            de=i.get('details') or {}
            csvw.writerow([i['name'],de.get('product_name',''),de.get('main',''),de.get('subtitle',''),de.get('theme',''),i['status'],'；'.join(i['warnings'])])
        z.writestr('卖点清单.csv','\ufeff'+b.getvalue())
        z.writestr('任务参数.json',json.dumps(job['settings'],ensure_ascii=False,indent=2))
    return FileResponse(dest,media_type='application/zip',filename=f'ZX_{jid}_电商主图.zip')
