"""Exercise the real web -> API -> PostgreSQL path; optionally verify after restart."""
import argparse
import json
from pathlib import Path
import urllib.error
import urllib.request
import uuid


def request(base, path, body=None):
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(base.rstrip('/') + path, data=data,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as response:
        return response.status, json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:3000')
    parser.add_argument('--save-receipt', type=Path)
    parser.add_argument('--expect-receipt', type=Path)
    args = parser.parse_args()
    measured = 0
    try:
        status, health = request(args.base_url, '/health/ready')
        assert status == 200 and health['status'] == 'ok', 'Web readiness failed'
        measured += 1
        if args.expect_receipt:
            note = json.loads(args.expect_receipt.read_text(encoding='utf-8'))
        else:
            text = 'persistence-check-' + uuid.uuid4().hex
            status, note = request(args.base_url, '/api/notes', {'text': text})
            assert status == 201 and note['text'] == text and note['id'] > 0, 'Insert failed'
            measured += 1
        status, notes = request(args.base_url, '/api/notes')
        assert status == 200 and any(row['id'] == note['id'] and row['text'] == note['text'] for row in notes), 'Stored note is missing'
        measured += 1
        if args.save_receipt:
            args.save_receipt.parent.mkdir(parents=True, exist_ok=True)
            args.save_receipt.write_text(json.dumps(note, ensure_ascii=False), encoding='utf-8')
        print('HARNESS_TEST_RESULT ' + json.dumps({'tests': measured, 'failures': 0, 'errors': 0, 'skipped': 0}))
        return 0
    except (AssertionError, OSError, ValueError, KeyError) as exc:
        print('Stack check failed: ' + str(exc))
        print('HARNESS_TEST_RESULT ' + json.dumps({'tests': measured + 1, 'failures': 1, 'errors': 0, 'skipped': 0}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
