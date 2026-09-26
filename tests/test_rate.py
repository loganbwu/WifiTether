"""Tests for writing star ratings to sidecar .xmp files from the viewer."""

from server import get_file_rating, process_file, state, write_xmp_rating


# ---------------------------------------------------------------------------
# write_xmp_rating
# ---------------------------------------------------------------------------

def test_write_creates_sidecar(tmp_path):
    photo = tmp_path / 'IMG_0001.CR3'
    photo.write_bytes(b'')
    write_xmp_rating(photo, 4)
    assert (tmp_path / 'IMG_0001.xmp').exists()
    assert get_file_rating(photo) == 4


def test_write_updates_attribute_form_and_preserves_other_tags(tmp_path):
    photo = tmp_path / 'IMG_0001.CR3'
    photo.write_bytes(b'')
    sidecar = tmp_path / 'IMG_0001.xmp'
    sidecar.write_text(
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF>'
        '<rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/"'
        ' xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"'
        ' xmp:Rating="2" crs:Exposure2012="+0.50"/>'
        '</rdf:RDF></x:xmpmeta>'
    )
    write_xmp_rating(photo, 5)
    content = sidecar.read_text()
    assert get_file_rating(photo) == 5
    assert content.count('xmp:Rating') == 1
    assert 'crs:Exposure2012="+0.50"' in content


def test_write_updates_element_form(tmp_path):
    photo = tmp_path / 'IMG_0001.CR3'
    photo.write_bytes(b'')
    sidecar = tmp_path / 'IMG_0001.xmp'
    sidecar.write_text(
        '<rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/">'
        '<xmp:Rating>3</xmp:Rating></rdf:Description>'
    )
    write_xmp_rating(photo, 0)
    assert get_file_rating(photo) == 0
    assert sidecar.read_text().count('<xmp:Rating>') == 1


def test_write_adds_rating_to_self_closing_description_without_xmp_namespace(tmp_path):
    photo = tmp_path / 'IMG_0001.CR3'
    photo.write_bytes(b'')
    sidecar = tmp_path / 'IMG_0001.xmp'
    sidecar.write_text(
        '<rdf:RDF><rdf:Description rdf:about=""'
        ' xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"'
        ' crs:Exposure2012="+0.50"/></rdf:RDF>'
    )
    write_xmp_rating(photo, 3)
    content = sidecar.read_text()
    assert get_file_rating(photo) == 3
    assert 'xmlns:xmp="http://ns.adobe.com/xap/1.0/"' in content
    assert 'crs:Exposure2012="+0.50"' in content


# ---------------------------------------------------------------------------
# /api/rate
# ---------------------------------------------------------------------------

def test_api_rate_writes_sidecar_and_updates_state(client, make_jpeg):
    path = make_jpeg('session1/photo.jpg', flash=1)
    state['folder'] = str(path.parent.parent)   # reset by the clean_state fixture
    process_file(str(path))

    r = client.post('/api/rate', json={'filename': 'session1/photo.jpg', 'rating': 4})
    assert r.status_code == 200
    assert r.get_json()['rating'] == 4
    assert get_file_rating(path) == 4
    assert state['series'][0]['base']['rating'] == 4


def test_api_rate_rejects_out_of_range(client):
    r = client.post('/api/rate', json={'filename': 'photo.jpg', 'rating': 6})
    assert r.status_code == 400


def test_api_rate_unknown_photo(client):
    r = client.post('/api/rate', json={'filename': 'nope.jpg', 'rating': 3})
    assert r.status_code == 404
