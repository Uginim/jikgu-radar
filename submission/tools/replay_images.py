"""Image identity is preserved by mapping transformations, not by redistributing packages."""
import base64
import hashlib
import io
from PIL import Image, ImageDraw, ImageOps


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encode(im, fmt, **kwargs):
    buf = io.BytesIO()
    im.save(buf, format=fmt, **kwargs)
    return buf.getvalue()


def variants(raw):
    yield 'original', raw
    with Image.open(io.BytesIO(raw)) as img:
        base = ImageOps.exif_transpose(img)
        thumb = base.copy()
        thumb.thumbnail((448, 448), Image.Resampling.LANCZOS)
        yield 'preview', encode(thumb.convert('RGB'), 'JPEG', quality=88)
        for angle in (90, 180, 270):
            yield f'rotate{angle}', encode(base.rotate(angle, expand=True).convert('RGB'), 'JPEG', quality=95)


def reference(info):
    ident = info['image_sha256']
    image = Image.new('RGB', (info['width'], info['height']), 'white')
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, image.width // 4, image.height // 4), fill='#' + ident[:6])
    draw.text((20, image.height // 3), 'Recorded visual input / ' + ident[:16], fill='black')
    return encode(image, info['format'])


def data_url(raw, fmt):
    return 'data:image/' + fmt.lower() + ';base64,' + base64.b64encode(raw).decode('ascii')
