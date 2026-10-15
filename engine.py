"""ZX AI Commerce Studio: compositing, conservative cutout and optional AI calls."""
from __future__ import annotations
import base64, colorsys, io, json, math, os, random, re
from collections import deque
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

ROOT = Path(__file__).resolve().parent
FONT_SERIF = next((p for p in [
    '/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc',
    '/usr/share/fonts/truetype/arphic/ukai.ttc',
    'C:/Windows/Fonts/simsun.ttc'] if os.path.exists(p)), None)
FONT_SANS = next((p for p in [
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    'C:/Windows/Fonts/msyh.ttc'] if os.path.exists(p)), FONT_SERIF)

def font(size:int, variant='serif', custom=None):
    path = custom if custom and Path(custom).is_file() else (FONT_SERIF if variant=='serif' else FONT_SANS)
    if path:
        return ImageFont.truetype(path, max(10,int(size)))
    return ImageFont.load_default()

def keyword_category(s):
    s=s.lower()
    d={
      'tea': ['茶','东方树叶','茉莉','乌龙','绿茶','red tea','tea'],
      'dairy':['奶','酸奶','乳','yogurt','milk'],
      'drink':['饮料','可乐','果汁','橙汁','咖啡','水','汽水','coffee','drink'],
      'snack':['薯','饼干','零食','蛋','肠','坚果','方便面','面包','糖','脯','核桃','chocolate','cookie','noodle'],
      'care':['湿巾','洗发','洗护','纸巾','沐浴','牙膏','浴巾','毛巾','护肤','纸','面膜','shampoo','tissue'],
      'home':['雨伞','手套','垃圾袋','收纳','拖鞋','盒','杯','电热毯','umbrella'],
      'tech':['充电','剃须刀','耳机','电源','数据线','蓝牙','charger','usb']}
    for k,v in d.items():
        if any(x in s for x in v):return k
    return 'other'

PALETTE={
    'tea':('#f6f0d9','#dbe4aa','#2f4e37','茉莉清香\n茶韵悠长','原叶萃取 · 清新茶香 · 鲜爽回甘','jasmine tea flowers and sunlit green leaves'),
    'dairy':('#f4f6ee','#dbe9d9','#315d52','醇香好奶\n鲜活口感','细腻奶香 · 顺滑入口 · 日常好滋味','soft creamy white milk splash and gentle meadows'),
    'drink':('#fff3dd','#ffd9a8','#785026','清爽好味\n活力随行','轻松畅饮 · 风味清新 · 快乐加倍','refreshing fruit slices and sparkling droplets'),
    'snack':('#fff0d9','#eecba3','#7a3e26','香酥好味\n越吃越香','香气诱人 · 口感丰富 · 休闲分享','warm snack table natural soft golden sunlight'),
    'care':('#f0f7f7','#d6eaec','#375d62','洁净呵护\n舒适随行','日常清洁 · 使用方便 · 贴心之选','fresh bathroom spa folded white towels and delicate water ripples'),
    'home':('#f6f3ed','#e8e0d1','#555b4d','贴心好物\n自在生活','简单实用 · 居家常备 · 轻巧便携','minimal cozy home with beige linen and sunlight'),
    'tech':('#eaf0f6','#cbd7ea','#253b56','便捷生活\n轻松随行','简约设计 · 日常实用 · 轻巧便携','minimal futuristic clean glass tech pedestal and soft reflections'),
    'other':('#f4f0e9','#e4dfd3','#435449','品质好物\n悦享生活','精选好物 · 轻松体验 · 日常相伴','minimal luxury lifestyle natural light')}

# Non-API previews intentionally do NOT pretend to contain AI-authored product facts.
def fallback_details(filename):
    name = re.sub(r'[_-]+',' ',Path(filename).stem).strip()[:45]
    cat=keyword_category(name)
    v=PALETTE[cat]
    return dict(product_name=name, category=cat, main=v[3], subtitle=v[4], theme=v[2], scene=v[5], copy_source='示例文案（未由AI核验）')

def image_to_data_uri(path, maxside=1100):
    im=Image.open(path).convert('RGB')
    im.thumbnail((maxside,maxside))
    b=io.BytesIO();im.save(b,format='JPEG',quality=84)
    return 'data:image/jpeg;base64,'+base64.b64encode(b.getvalue()).decode()

def recognize_product(path, key=None, original_name=None):
    if not key:
        return fallback_details(original_name or path.name)
    from openai import OpenAI
    client=OpenAI(api_key=key, timeout=90)
    prompt='''你是电商主图审稿与文案专家。请识别图中产品，结合能从包装直接确认的信息输出一个JSON对象，字段：product_name, category, main, subtitle, theme, scene。
category只能为tea/dairy/drink/snack/care/home/tech/other。
main为有画面感且简洁的中文主卖点，建议8到14个字，用换行符\\n分成两行，每行不超过7字。subtitle为三个短卖点，中间用“ · ”分隔，整句不超过23个汉字。
theme为深色十六进制文字色彩如#2f4e37。scene为80词以内的英文电商摄影背景描述，严格禁止背景有商品、包装、logo和任何文字。版式要求：左中留白供文字排版，右侧留白供后期合成实物。
**严禁虚构包装未显示的信息**：不要编造温度、时长、百分比、健康功效、配料、容量、资质、认证；看不清时使用不涉及实际功能的感官和场景词。
只回复JSON，不要markdown。'''
    r=client.responses.create(model=os.getenv('OPENAI_VISION_MODEL','gpt-4.1-mini'),input=[{'role':'user','content':[{'type':'input_text','text':prompt},{'type':'input_image','image_url':image_to_data_uri(path)}]}])
    t=r.output_text.strip(); t=re.sub(r'^```(?:json)?\s*|\s*```$','',t)
    m=re.search(r'\{.*\}',t,re.S)
    if not m:raise ValueError('商品识别结果不是JSON')
    d=json.loads(m.group())
    seed=fallback_details(original_name or path.name)
    for k in ['product_name','category','main','subtitle','theme','scene']:
        if k in d and isinstance(d[k],str) and d[k].strip():seed[k]=d[k].strip()
    if seed['category'] not in PALETTE: seed['category']='other'
    if not re.fullmatch(r'#[0-9a-fA-F]{6}',seed['theme']):seed['theme']=PALETTE[seed['category']][2]
    seed['main']=seed['main'].replace('\\n','\n')[:35]
    seed['subtitle']=seed['subtitle'][:65]
    seed['copy_source']='AI候选文案（请人工复核产品事实）'
    return seed

def demo_background(size,category,seed=0):
    # A locally-created fallback background, not an AI-generated photograph.
    import numpy as np
    w=h=size
    cat=PALETTE.get(category,PALETTE['other'])
    def rgb(h):return tuple(int(h[i:i+2],16) for i in (1,3,5))
    a,b=rgb(cat[0]),rgb(cat[1]);rng=random.Random(seed)
    yy,xx=np.mgrid[0:h,0:w];u=(xx/w*.45+yy/h*.55)
    warmth=(1-np.clip(u,0,1))[:,:,None]
    base=np.asarray(a)[None,None,:]*warmth+np.asarray(b)[None,None,:]*(1-warmth)
    radial=((xx-.82*w)**2+(yy-.17*h)**2)**.5/w
    base=np.clip(base+np.maximum(0,1-radial*1.6)[:,:,None]*13,0,255).astype('uint8')
    im=Image.fromarray(base,'RGB').convert('RGBA')
    deco=Image.new('RGBA',(w,h));d=ImageDraw.Draw(deco,'RGBA')
    for i in range(65):
        px=rng.randint(0,w);py=rng.randint(0,h)
        r=rng.randint(max(4,w//130),max(12,w//30))
        d.ellipse((px-r,py-r,px+r,py+r),fill=(255,255,235,rng.randint(35,110)))
    deco=deco.filter(ImageFilter.GaussianBlur(w/90));im=Image.alpha_composite(im,deco)
    dr=ImageDraw.Draw(im,'RGBA')
    for i in range(9):
        x=rng.randint(-60,w+60);y=rng.choice([rng.randint(0,int(h*.2)),rng.randint(int(h*.76),h+20)])
        rad=rng.randint(w//60,w//27)
        c=(245,255,226,150) if category in ('tea','dairy') else (255,246,232,130)
        for j in range(5):
            theta=j*2*math.pi/5
            dx=math.cos(theta)*rad;dy=math.sin(theta)*rad
            dr.ellipse((x+dx-rad*.48,y+dy-rad*.48,x+dx+rad*.48,y+dy+rad*.48),fill=c)
        dr.ellipse((x-rad*.2,y-rad*.2,x+rad*.2,y+rad*.2),fill=(245,210,105,135))
    dr.ellipse((int(w*.50),int(h*.80),int(w*1.35),int(h*1.16)),fill=(255,255,250,55))
    return im.convert('RGB')

def ai_background(size, details, outpath, key, img_model='gpt-image-1.5', quality='medium'):
    from openai import OpenAI
    client=OpenAI(api_key=key,timeout=240)
    category=details['category']; scene=details['scene']
    prompt=f'''Generate a premium, photorealistic 1:1 e-commerce commercial photography BACKGROUND PLATE ONLY. Product category: {category}. Visual story: {scene}.
Composition: the middle-left region covering 10%-49% width and 22%-58% height MUST have calm, bright, unfussy negative space for Chinese headline and subtitle applied later. The region x=52%-84%, y=12%-86% MUST remain usable as staging area for a product photograph composited later. No hero product should be present. Natural coherent lighting; beautiful elegant surfaces with restrained floral / ingredient accents around perimeter and bottom, subtle depth of field, premium photographic realism. Minimal bright cream-palette atmosphere, soft shadows. Strictly NO TEXT, no logo, no watermark, no words, no bottles, no boxes, no cans, no packages, no fake products, no graphics or labels. Produce only the blank photographic set.''' 
    res=client.images.generate(model=img_model,prompt=prompt,size='1024x1024',quality=quality,n=1)
    if not res.data or not res.data[0].b64_json:raise RuntimeError('生图接口未返回图片')
    img=Image.open(io.BytesIO(base64.b64decode(res.data[0].b64_json))).convert('RGB').resize((size,size),Image.Resampling.LANCZOS)
    img.save(outpath)
    return img

def transparency_cutout(img):
    import numpy as np
    from scipy import ndimage as ndi
    rgb=img.convert('RGB'); ar=np.asarray(rgb).astype('float32')
    h,w=ar.shape[:2]
    corners=np.concatenate([ar[:max(2,h//18),:max(2,w//18)].reshape(-1,3),ar[:max(2,h//18),-max(2,w//18):].reshape(-1,3),ar[-max(2,h//18):,:max(2,w//18)].reshape(-1,3),ar[-max(2,h//18):,-max(2,w//18):].reshape(-1,3)])
    c=np.median(corners,axis=0)
    uniform=float(np.median(np.abs(corners-c)))<11
    # Do not silently destroy products photographed against complex backgrounds.
    if not uniform:return None, '背景复杂，未自动抠图；建议上传透明PNG或安装rembg'
    dist=np.sqrt(np.sum((ar-c)**2,axis=2))
    candidate=dist<max(24, 0.07*255)
    lab,n=ndi.label(candidate)
    edge=np.unique(np.concatenate([lab[0],lab[-1],lab[:,0],lab[:,-1]])); edge=edge[edge>0]
    bg=np.isin(lab,edge)
    bg=ndi.binary_dilation(bg,iterations=max(1,min(h,w)//600))
    alpha=(~bg).astype('uint8')*255
    alpha=Image.fromarray(alpha).filter(ImageFilter.GaussianBlur(max(.5,min(h,w)/850)))
    out=rgb.convert('RGBA');out.putalpha(alpha)
    bbox=out.getbbox()
    if not bbox or ((bbox[2]-bbox[0])*(bbox[3]-bbox[1])<w*h*.015):return None,'自动抠图不可靠，需透明PNG或rembg'
    return out.crop(bbox),None

def isolate_product(path, cutout_path):
    im=Image.open(path);im.thumbnail((2500,2500),Image.Resampling.LANCZOS)
    if im.mode in ('RGBA','LA') and im.getextrema()[-1][0]<240:
        cut=im.convert('RGBA');cut=cut.crop(cut.getbbox());cut.save(cutout_path);return cut,None
    try:
        import rembg
        buf=io.BytesIO();im.save(buf,format='PNG')
        cut=Image.open(io.BytesIO(rembg.remove(buf.getvalue()))).convert('RGBA');cut=cut.crop(cut.getbbox())
        if cut.getbbox():cut.save(cutout_path);return cut,None
    except (ImportError,ModuleNotFoundError):pass
    except Exception as exc:pass
    cut,warning=transparency_cutout(im)
    if cut is not None:cut.save(cutout_path)
    return cut,warning

def _fit_draw(draw,text,x,y,max_width,size,fill,variant='serif',lineheight=1.12,max_lines=2):
    txt=[s.strip() for s in text.replace('\\n','\n').splitlines() if s.strip()]
    if not txt:txt=['品质好物']
    if len(txt)==1 and len(txt[0])>=7:
        a=txt[0];pivot=math.ceil(len(a)/2);txt=[a[:pivot],a[pivot:]]
    if len(txt)>max_lines:txt=txt[:max_lines-1]+[''.join(txt[max_lines-1:])]
    size=int(size)
    for s in range(size,max(14,size//2),-1):
        f=font(s,variant)
        if max(draw.textbbox((0,0),t,font=f)[2] for t in txt)<=max_width:break
    step=int(s*lineheight)
    for t in txt:
        draw.text((x,y),t,font=f,fill=fill,stroke_width=0)
        y+=step
    return y,s

def _color_ok(color, fallback):
    return color if isinstance(color,str) and re.fullmatch('#[0-9a-fA-F]{6}',color) else fallback

def compose_image(background_path, cutout_path, output_path, details, settings, logo_path=None):
    size=int(settings.get('size',1125));size=max(640,min(2400,size));S=size/1125
    bg=Image.open(background_path).convert('RGBA').resize((size,size),Image.Resampling.LANCZOS)
    overlay=Image.new('RGBA',(size,size),(255,255,255,0));d=ImageDraw.Draw(overlay)
    # gentle left vignette ensures readability over photographic backgrounds.
    veil=Image.new('RGBA',(size,size)); v=ImageDraw.Draw(veil)
    for i in range(int(size*.57),-1,-3):
        opacity=int(35 * (1-i/(size*.57))**1.5)
        v.rectangle((0,0,i,size),fill=(255,255,247,opacity))
    bg=Image.alpha_composite(bg,veil)
    theme=_color_ok(details.get('theme'),PALETTE.get(details['category'],PALETTE['other'])[2])
    main=details.get('main','品质好物\n悦享生活')
    subtitle=details.get('subtitle','精选好物 · 日常相伴')
    if logo_path and Path(logo_path).exists():
        logo=Image.open(logo_path).convert('RGBA');logo.thumbnail((int(size*.235),int(size*.14)),Image.Resampling.LANCZOS)
        overlay.alpha_composite(logo,(int(size*.071),int(size*.063)))
    x=int(size*.105);y=int(size*.265)
    y,lastsize=_fit_draw(d,main,x,y,int(size*.41),int(settings.get('headline_px',70))*S,theme,variant='serif',lineheight=1.22)
    line_y=y+int(19*S)
    d.rounded_rectangle((x,line_y,x+int(66*S),line_y+int(4*S)),radius=int(2*S),fill=(205,169,117,230))
    sy=line_y+int(30*S)
    sf=font(int(settings.get('subtitle_px',26))*S,'serif')
    # wrap subtitle only at bullet separators to avoid clipping
    text=subtitle.replace('•','·').strip()
    if d.textbbox((0,0),text,font=sf)[2]>size*.43:
        chunks=[x.strip() for x in text.split('·')]
        if len(chunks)>1:
            text=' · '.join(chunks[:2])+'\n'+' · '.join(chunks[2:])
    for line in text.splitlines()[:3]:
        for st in range(int(settings.get('subtitle_px',26)*S),int(12*S),-1):
            f=font(st,'serif')
            if d.textbbox((0,0),line,font=f)[2]<size*.43:break
        d.text((x,sy),line,font=f,fill=theme)
        sy+=int((st+10*S))
    bg=Image.alpha_composite(bg,overlay)
    warnings=[]
    if cutout_path and Path(cutout_path).exists():
        pr=Image.open(cutout_path).convert('RGBA')
        # right region: may need wider layout for landscape products
        landscape=(pr.width/max(1,pr.height))>1.25
        if landscape:
            box=(int(size*.53),int(size*.31),int(size*.96),int(size*.81))
        else:
            box=(int(size*.53),int(size*.11),int(size*.85),int(size*.86))
        iw=box[2]-box[0];ih=box[3]-box[1]
        pr.thumbnail((iw,ih),Image.Resampling.LANCZOS)
        px=box[0]+(iw-pr.width)//2;py=box[1]+(ih-pr.height)//2
        shadow=Image.new('RGBA',(size,size));sh=Image.new('RGBA',pr.size,(0,0,0,0));sh.putalpha(pr.getchannel('A').point(lambda x: int(x*.25)))
        shadow.alpha_composite(sh,(px+int(9*S),py+int(19*S)))
        shadow=shadow.filter(ImageFilter.GaussianBlur(int(22*S)))
        bg=Image.alpha_composite(bg,shadow)
        bg.alpha_composite(pr,(px,py))
    else:
        warnings.append('商品未能可靠抠图：为避免改变包装，成品暂不合成商品。请上传透明PNG或安装rembg后重新生成。')
    bg.convert('RGB').save(output_path,format='PNG',optimize=True)
    return warnings
