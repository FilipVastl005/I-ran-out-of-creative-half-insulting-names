"""
mobile_stream_api.py
--------------------
Dedicated FastAPI endpoint for high-performance, direct-play mobile video streaming.
Implements RFC 7233 byte-range specifications strictly compatible with iOS (AVPlayer)
and Android (ExoPlayer).

Features:
- Byte-range request parsing: open-ended ('bytes=0-'), closed ('bytes=0-102400'),
  and suffix-based ('bytes=-500').
- HTTP 206 Partial Content with precise Content-Range, Content-Length, and Accept-Ranges.
- Chunked disk I/O using Python file seeking (file.seek(start)) and 64KB read buffers.
- Capped memory usage: defaults to 1MB chunk size if the client's end range is open-ended.
- Zero transcoding: streams raw disk bytes directly to the socket.
"""

import os
import re
import mimetypes
from typing import Optional, Generator, Tuple

from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import StreamingResponse, Response

app = FastAPI(title="Mobile Video Streaming Engine")

# 1MB chunk size when client sends open-ended range ('bytes=10000-')
DEFAULT_CHUNK_SIZE: int = 1024 * 1024

# 64KB internal disk read buffer to avoid RAM bloat during chunk transmission
READ_BUFFER_SIZE: int = 64 * 1024

# Regex pattern for RFC 7233 Byte Range headers
RANGE_REGEX = re.compile(r"^bytes=(\d*)-(\d*)$")


# --------------------------------------------------------------------------- #
# Mock Database Service Layer
# --------------------------------------------------------------------------- #
def get_video_record_from_db(video_id: str) -> Optional[dict]:
    """
    Mock database resolver.
    In production, replace with SQLAlchemy, Tortoise-ORM, or raw async DB query:
    e.g. video = await db.fetch_one("SELECT * FROM media WHERE id = :id", {"id": video_id})
    """
    mock_db = {
        "sample-1080p": {
            "id": "sample-1080p",
            "title": "Big Buck Bunny 1080p",
            # Point to an existing local file or relative media root
            "file_path": os.path.abspath(
                os.path.join(os.path.dirname(__file__), "media", "videos", "sample.mp4")
            ),
            "mime_type": "video/mp4",
        },
        "mobile-demo": {
            "id": "mobile-demo",
            "title": "Mobile Demo Stream",
            "file_path": "/var/media/videos/demo.mp4",
            "mime_type": "video/mp4",
        },
    }
    return mock_db.get(video_id)


# --------------------------------------------------------------------------- #
# Byte-Range Parsing Engine
# --------------------------------------------------------------------------- #
def parse_byte_range(
    range_header: str, file_size: int, default_chunk_size: int = DEFAULT_CHUNK_SIZE
) -> Tuple[int, int]:
    """
    Parses and validates an HTTP Range header according to RFC 7233.

    Handles three valid forms:
      1. 'bytes=0-102400'   -> Explicit start and end
      2. 'bytes=1048576-'   -> Open-ended (capped by default_chunk_size or file_size)
      3. 'bytes=-524288'    -> Suffix range (final N bytes of the file)

    Returns:
        Tuple[int, int]: (start_byte, end_byte) inclusive.

    Raises:
        HTTPException(416): If the requested range cannot be satisfied.
    """
    range_header = range_header.strip()
    match = RANGE_REGEX.match(range_header)

    if not match:
        raise HTTPException(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            detail="Invalid Range header format.",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    start_str, end_str = match.groups()

    if start_str and end_str:
        # Form 1: Explicit closed range ('bytes=0-102400')
        start = int(start_str)
        end = int(end_str)
        if end >= file_size:
            end = file_size - 1

    elif start_str and not end_str:
        # Form 2: Open-ended range ('bytes=1048576-')
        # Crucial for iOS/Android: cap the end byte to prevent buffering entire multi-GB files into RAM
        start = int(start_str)
        end = min(start + default_chunk_size - 1, file_size - 1)

    elif not start_str and end_str:
        # Form 3: Suffix-byte range ('bytes=-524288' -> last 512KB of file)
        suffix_length = int(end_str)
        if suffix_length <= 0:
            raise HTTPException(
                status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
                detail="Suffix length must be greater than zero.",
                headers={"Content-Range": f"bytes */{file_size}"},
            )
        start = max(0, file_size - suffix_length)
        end = file_size - 1

    else:
        # 'bytes=-' is invalid
        raise HTTPException(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            detail="Range header requires start or end offset.",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    # Range boundaries check
    if start >= file_size or start > end:
        raise HTTPException(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            detail="Requested byte range outside file boundaries.",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    return start, end


# --------------------------------------------------------------------------- #
# Chunked Disk I/O Streaming Generator
# --------------------------------------------------------------------------- #
def ranged_file_iterator(
    file_path: str, start: int, end: int, buffer_size: int = READ_BUFFER_SIZE
) -> Generator[bytes, None, None]:
    """
    Generator that performs chunked file reading directly from disk without loading
    the full range or file into memory.

    Seeks directly to `start` byte and yields successive buffers of at most `buffer_size`
    until `end` is reached.
    """
    bytes_to_read = (end - start) + 1
    file_obj = open(file_path, "rb")

    try:
        file_obj.seek(start)
        while bytes_to_read > 0:
            current_read_size = min(buffer_size, bytes_to_read)
            data = file_obj.read(current_read_size)
            if not data:
                break
            bytes_to_read -= len(data)
            yield data
    finally:
        file_obj.close()


# --------------------------------------------------------------------------- #
# High-Performance Video Streaming Endpoint
# --------------------------------------------------------------------------- #
@app.get(
    "/api/videos/{video_id}/stream",
    summary="High-Performance Direct-Play Mobile Video Stream",
    responses={
        200: {"description": "Full video content (when client sends no Range header)"},
        206: {"description": "Partial content successfully served for mobile player"},
        404: {"description": "Video not found"},
        416: {"description": "Range not satisfiable"},
    },
)
def stream_video(video_id: str, request: Request):
    """
    Direct-play streaming endpoint with full HTTP 206 Partial Content byte-range support.

    iOS (AVPlayer) & Android (ExoPlayer) Lifecycle Compatibility:
      1. Probe Phase: AVPlayer issues an initial probe ('Range: bytes=0-1' or 'bytes=0-')
         to parse container headers (moov atom in MP4).
      2. Seek Phase: User seeks along timeline; player issues arbitrary offset ranges.
      3. Scrubber Phase: Rapid seeks require instant 206 responses with exact Content-Range.
    """
    # 1. Resolve video file path from database
    video_record = get_video_record_from_db(video_id)
    if not video_record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Video ID '{video_id}' not found.",
        )

    file_path: str = video_record["file_path"]

    # 2. Verify file presence on filesystem
    if not os.path.isfile(file_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video asset file is missing from local storage.",
        )

    total_file_size: int = os.path.getsize(file_path)

    # 3. Determine MIME Content-Type
    content_type: str = (
        video_record.get("mime_type")
        or mimetypes.guess_type(file_path)[0]
        or "video/mp4"
    )

    # 4. Inspect HTTP Range request header
    range_header: Optional[str] = request.headers.get("range") or request.headers.get("Range")

    # Fallback: Client did not supply Range header -> Stream starting from byte 0
    if not range_header:
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(total_file_size),
            "Content-Type": content_type,
            "Cache-Control": "public, max-age=3600",
        }
        return StreamingResponse(
            ranged_file_iterator(file_path, 0, total_file_size - 1),
            status_code=status.HTTP_200_OK,
            headers=headers,
            media_type=content_type,
        )

    # 5. Parse and validate byte range
    start_byte, end_byte = parse_byte_range(range_header, total_file_size)
    content_length: int = (end_byte - start_byte) + 1

    # 6. Build required RFC 7233 response headers
    response_headers = {
        "Content-Range": f"bytes {start_byte}-{end_byte}/{total_file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(content_length),
        "Content-Type": content_type,
        "Cache-Control": "no-cache, no-store, must-revalidate",
    }

    # 7. Return 206 Partial Content with chunked disk generator
    return StreamingResponse(
        ranged_file_iterator(file_path, start_byte, end_byte),
        status_code=status.HTTP_206_PARTIAL_CONTENT,
        headers=response_headers,
        media_type=content_type,
    )


# --------------------------------------------------------------------------- #
# Optional HTTP HEAD Probe Endpoint
# --------------------------------------------------------------------------- #
@app.head("/api/videos/{video_id}/stream", summary="Video Metadata Probe")
def probe_video_stream(video_id: str):
    """
    Enables mobile media frameworks to probe video file size and byte-range
    support without transmitting payload bytes.
    """
    video_record = get_video_record_from_db(video_id)
    if not video_record or not os.path.isfile(video_record["file_path"]):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    file_path = video_record["file_path"]
    total_size = os.path.getsize(file_path)
    content_type = video_record.get("mime_type") or "video/mp4"

    return Response(
        status_code=status.HTTP_200_OK,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(total_size),
            "Content-Type": content_type,
        },
    )


if __name__ == "__main__":
    import uvicorn
    # To run locally: uvicorn mobile_stream_api:app --host 0.0.0.0 --port 8000 --reload
    uvicorn.run(app, host="0.0.0.0", port=8000)
