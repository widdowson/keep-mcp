import ipaddress
import os
import socket
import struct
from functools import wraps
from threading import RLock
from urllib.parse import urlparse

import gkeepapi
import requests
from dotenv import load_dotenv
from mcp.server.mcpserver.exceptions import ToolError

from . import formatting, raw_nodes

KEEP_MCP_LABEL = "keep-mcp"

# Record the raw sync JSON per node, for get_note_raw and formatting lookups.
raw_nodes.install()

_keep_client = None
_client_lock = RLock()


def keep_operation(func):
    """Serialize access to the shared Keep tree, including mutation and sync."""
    @wraps(func)
    def wrapped(*args, **kwargs):
        global _keep_client
        with _client_lock:
            try:
                return func(*args, **kwargs)
            except ToolError:
                raise
            except Exception:
                # A failed operation can leave unsaved or prematurely cleaned
                # mutations in memory. Reload remote state on the next call.
                _keep_client = None
                raise
    return wrapped


@keep_operation
def get_client():
    """
    Get a freshly synced Google Keep client.
    Reuse the authenticated client until an operation fails.
    
    Returns:
        gkeepapi.Keep: Authenticated Keep client
    """
    global _keep_client
    
    if _keep_client is not None:
        _keep_client.sync()
        return _keep_client
    
    # Load environment variables
    load_dotenv()
    
    # Get credentials from environment variables
    email = os.getenv('GOOGLE_EMAIL')
    master_token = os.getenv('GOOGLE_MASTER_TOKEN')
    
    if not email or not master_token:
        raise ToolError("Missing Google Keep credentials. Please set GOOGLE_EMAIL and GOOGLE_MASTER_TOKEN environment variables.")
    
    # Initialize the Keep API
    raw_nodes.reset()
    formatting.reader.reset()
    keep = gkeepapi.Keep()
    
    # Authenticate
    try:
        keep.authenticate(email, master_token)
    except requests.exceptions.JSONDecodeError as exc:
        raise ToolError(
            "Google Keep API returned a non-JSON response during authentication. "
            "This usually means the unofficial Keep API (notes/v1) is inaccessible "
            "from this environment (HTTP 403/4xx). "
            "Check that your GOOGLE_MASTER_TOKEN is valid and that the Keep API "
            "is reachable from this network."
        ) from exc
    except gkeepapi.exception.LoginException as exc:
        raise ToolError(
            f"Google Keep login failed: {exc}. "
            "Verify that GOOGLE_EMAIL and GOOGLE_MASTER_TOKEN are correct."
        ) from exc
    
    # Store the client for reuse
    _keep_client = keep
    
    return keep

def serialize_label(label):
    return {'id': label.id, 'name': label.name}


def serialize_list_item(item, item_formatting=None):
    payload = {
        'id': item.id,
        'text': item.text,
        'checked': item.checked,
        'parent_item_id': item.parent_item.id if item.parent_item else None,
    }
    if item_formatting:
        payload['formatting'] = item_formatting
    return payload


def _raw_updated(note):
    raw = raw_nodes.raw_json(note) or {}
    return (raw.get('timestamps') or {}).get('updated')


def note_formatting(keep, notes, strict=False):
    """
    Look up rich-text formatting for notes, keyed by note id then item id.

    Plain notes and items are left out. An item whose model text no longer
    matches its synced text is left out too, rather than shown misaligned.
    The note body of a plain note is keyed by ''. Formatting is a read-only
    extra: when the model can't be fetched, notes come back without it,
    unless strict is true, when the error is raised as a ToolError.
    """
    pairs = [(note.id, _raw_updated(note)) for note in notes]
    pairs = [(note_id, updated) for note_id, updated in pairs if updated]
    if not pairs:
        return {}
    try:
        return _describe_notes(notes, formatting.reader.for_notes(keep, pairs))
    except Exception as exc:
        formatting.logger.warning('Could not fetch Keep formatting', exc_info=True)
        if strict:
            raise ToolError(
                f'Could not check the note for rich-text formatting ({exc}). '
                'Pass discard_formatting=true to edit the text anyway.'
            ) from exc
        return {}


def _describe_notes(notes, models):
    result = {}
    for note in notes:
        parsed_items = models.get(note.id)
        if not parsed_items:
            continue
        texts = {item.id: item.text for item in getattr(note, 'items', [])}
        if not hasattr(note, 'items'):
            texts[''] = note.text
        described = {}
        for item_id, parsed in parsed_items.items():
            if texts.get(item_id) != parsed['text']:
                continue
            info = formatting.describe(parsed)
            if info:
                described[item_id] = info
        if described:
            result[note.id] = described
    return result


def formatting_for(keep, note, strict=False):
    """Return {item id: formatting} for one note (see note_formatting)."""
    return note_formatting(keep, [note], strict=strict).get(note.id, {})


def serialize_note(note, note_fmt=None):
    """
    Serialize a Google Keep note into a dictionary.
    
    Args:
        note: A Google Keep note object
        note_fmt: Optional {item id: formatting} from note_formatting()
        
    Returns:
        dict: A dictionary containing the note's id, title, text, pinned status, color and labels
    """
    timestamps = getattr(note, 'timestamps', None)
    created = getattr(timestamps, 'created', None)
    updated = getattr(timestamps, 'updated', None)

    payload = {
        'id': note.id,
        'title': note.title,
        'text': note.text,
        'type': note.type.value,
        'pinned': note.pinned,
        'archived': note.archived,
        'trashed': note.trashed,
        'color': note.color.value if note.color else None,
        'created': created.isoformat() if created else None,
        'updated': updated.isoformat() if updated else None,
        'labels': [serialize_label(label) for label in note.labels.all()],
        'collaborators': list(note.collaborators.all()),
    }

    note_fmt = note_fmt or {}
    if hasattr(note, 'items'):
        payload['items'] = [
            serialize_list_item(item, note_fmt.get(item.id)) for item in note.items
        ]
    elif note_fmt.get(''):
        payload['formatting'] = note_fmt['']

    payload['media'] = [
        {
            'blob_id': blob.id,
            'type': blob.blob.type.value if blob.blob and blob.blob.type else None,
        }
        for blob in note.blobs
    ]

    return payload

_MEDIA_EXTENSIONS = {
    'image/png': '.png',
    'image/jpeg': '.jpg',
    'image/gif': '.gif',
    'image/webp': '.webp',
    'audio/3gpp': '.3gp',
    'audio/amr': '.amr',
    'audio/mpeg': '.mp3',
}


def media_extension(content_type):
    """
    Map a media response Content-Type to a file extension.

    Args:
        content_type: The Content-Type header value (may carry parameters)

    Returns:
        str: A dotted extension, '.bin' when the type is unknown or missing
    """
    if not content_type:
        return '.bin'
    return _MEDIA_EXTENSIONS.get(content_type.split(';')[0].strip().lower(), '.bin')


def fetch_blob_bytes(keep, blob):
    """
    Download a media blob through the authenticated Keep session.

    The links returned by getMediaLink() require Google authentication and
    answer 403 to plain HTTP clients, so the download rides the same session
    and credentials the server is already authenticated with.

    Args:
        keep: Authenticated gkeepapi.Keep client
        blob: A note media blob node

    Returns:
        tuple: (bytes, content_type) of the downloaded media
    """
    url = keep.getMediaLink(blob)
    media_api = keep._media_api
    response = media_api._send(url=url, method='GET')
    if response.status_code in (400, 401, 403):
        # Some media endpoints reject the OAuth header; retry bare on the same session.
        response = media_api._session.get(url)
    response.raise_for_status()
    return response.content, response.headers.get('Content-Type')


def is_unsafe_mode() -> bool:
    return os.getenv('UNSAFE_MODE', '').lower() == 'true'


def can_modify_note(note):
    """
    Check if a note can be modified based on label and environment settings.

    Args:
        note: A Google Keep note object

    Returns:
        bool: True if the note can be modified, False otherwise
    """
    return is_unsafe_mode() or has_keep_mcp_label(note)


def has_keep_mcp_label(note):
    """
    Check if a note has the keep-mcp label.

    Args:
        note: A Google Keep note object

    Returns:
        bool: True if the note has the keep-mcp label, False otherwise
    """
    return any(label.name == KEEP_MCP_LABEL for label in note.labels.all())


# --- Image upload -----------------------------------------------------------
#
# gkeepapi can read media but has no upload. The flow below follows the Keep
# Android app as documented in rukins/gkeepapi-java (server-endpoints.md):
#   1. sync a new BLOB node under the note, which gives it a serverId;
#   2. open a resumable upload at /upload/notes/v1/media/<blob serverId>;
#   3. PUT the image bytes to the returned Location;
#   4. sync the blob node again.

MEDIA_UPLOAD_URL = os.getenv(
    'KEEP_MEDIA_UPLOAD_URL', 'https://notes-pa.googleapis.com/upload/notes/v1/media/'
)
MAX_IMAGE_BYTES = 10 * 1024 * 1024
UPLOAD_TIMEOUT = 60


def image_info(data):
    """
    Read the MIME type and pixel size of a PNG, JPEG or GIF image.

    Args:
        data: The image bytes

    Returns:
        tuple: (mimetype, width, height)

    Raises:
        ToolError: When the bytes are not a supported image
    """
    if data.startswith(b'\x89PNG\r\n\x1a\n') and len(data) >= 24:
        width, height = struct.unpack('>II', data[16:24])
        return 'image/png', width, height
    if data[:6] in (b'GIF87a', b'GIF89a') and len(data) >= 10:
        width, height = struct.unpack('<HH', data[6:10])
        return 'image/gif', width, height
    if data.startswith(b'\xff\xd8'):
        size = _jpeg_size(data)
        if size:
            return ('image/jpeg', *size)
    raise ToolError('Unsupported image: send a PNG, JPEG or GIF')


# JPEG start-of-frame markers carry the image size; DHT/JPG/DAC do not.
_JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _jpeg_size(data):
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            return None
        marker = data[index + 1]
        if marker == 0xFF:
            index += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        (length,) = struct.unpack('>H', data[index + 2:index + 4])
        if marker in _JPEG_SOF:
            height, width = struct.unpack('>HH', data[index + 5:index + 9])
            return width, height
        index += 2 + length
    return None


def _ensure_public_https(url):
    """Refuse URLs that could reach the NAS's own network."""
    parsed = urlparse(url)
    if parsed.scheme != 'https' or not parsed.hostname:
        raise ToolError('image_url must be an https:// URL')
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ToolError(f'Could not resolve {parsed.hostname}') from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise ToolError('image_url must point to a public host')


def fetch_image_url(url):
    """
    Download an image from a public https URL, capped at MAX_IMAGE_BYTES.

    Redirects are not followed, so the public-host check cannot be bypassed.
    """
    _ensure_public_https(url)
    response = requests.get(url, timeout=UPLOAD_TIMEOUT, stream=True, allow_redirects=False)
    if response.is_redirect:
        raise ToolError('image_url redirected; pass the final URL')
    if response.status_code != 200:
        raise ToolError(f'image_url returned HTTP {response.status_code}')
    chunks = []
    total = 0
    for chunk in response.iter_content(64 * 1024):
        total += len(chunk)
        if total > MAX_IMAGE_BYTES:
            raise ToolError(f'Image is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB')
        chunks.append(chunk)
    return b''.join(chunks)


def _authed_request(keep, method, url, **kwargs):
    """Send a request with the Keep OAuth token, refreshing it once on 401."""
    auth = keep._keep_api.getAuth()
    headers = dict(kwargs.pop('headers', {}))
    for attempt in range(2):
        headers['Authorization'] = 'OAuth ' + auth.getAuthToken()
        response = requests.request(
            method, url, headers=headers, timeout=UPLOAD_TIMEOUT, **kwargs
        )
        if response.status_code != 401 or attempt:
            return response
        auth.refresh()
    return response


def _upload_error(step, response):
    body = (response.text or '')[:300]
    return ToolError(f'Keep image upload failed at {step}: HTTP {response.status_code} {body}')


def upload_image(keep, note, data):
    """
    Attach an image to a note through Keep's media upload endpoint.

    Args:
        keep: Authenticated gkeepapi.Keep client
        note: The note to attach the image to (must already exist on the server)
        data: The image bytes

    Returns:
        gkeepapi.node.Blob: The synced blob node
    """
    if len(data) > MAX_IMAGE_BYTES:
        raise ToolError(f'Image is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB')
    mimetype, width, height = image_info(data)
    if note.server_id is None:
        raise ToolError(f'Note {note.id} has not been synced to Keep yet')

    image = gkeepapi.node.NodeImage()
    image._mimetype = mimetype
    image._width = width
    image._height = height
    image._byte_size = len(data)
    image._extraction_status = 'PROCESSING_REQUESTED'
    blob = gkeepapi.node.Blob(parent_id=note.id)
    blob.blob = image
    note.append(blob, True)
    keep.sync()

    try:
        if blob.server_id is None:
            raise ToolError('Keep did not assign a server ID to the new image')

        start = _authed_request(
            keep,
            'POST',
            MEDIA_UPLOAD_URL + blob.server_id,
            params={'noteId': note.server_id, 'uploadType': 'resumable'},
            json={},
            headers={
                'X-Upload-Content-Type': mimetype,
                'X-Upload-Content-Length': str(len(data)),
            },
        )
        location = start.headers.get('Location') or start.headers.get('location')
        if start.status_code >= 300 or not location:
            raise _upload_error('start', start)

        put = _authed_request(
            keep, 'PUT', location, data=data, headers={'Content-Type': mimetype}
        )
        if put.status_code >= 300:
            raise _upload_error('upload', put)
    except Exception:
        # Don't leave an empty image placeholder on the note.
        blob.delete()
        keep.sync()
        raise

    blob.touch()
    keep.sync()
    return blob


def serialize_blob(blob):
    image = blob.blob
    return {
        'blob_id': blob.id,
        'type': image.type.value if image and image.type else None,
        'mimetype': getattr(image, '_mimetype', None),
        'width': getattr(image, 'width', None),
        'height': getattr(image, 'height', None),
        'byte_size': getattr(image, 'byte_size', None),
    }
