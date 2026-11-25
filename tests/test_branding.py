import io

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def test_only_administrators_change_branding(app, as_user):
    assert as_user("alice").get("/settings/branding").status_code == 403
    assert as_user("alice").post("/settings/branding", {"site_name": "Mine"}).status_code == 403
    assert as_user("admin").get("/settings/branding").status_code == 200


def test_branding_shows_everywhere(app, as_user):
    b = as_user("admin")
    r = b.post("/settings/branding", {"site_name": "OSG perfSONAR Meshes", "tagline": "WLCG and OSG",
                                      "accent": "#aa3300", "header": "#002244", "contact": "help@example.org",
                                      "about": "Run by the network team.\n\nDocs at https://example.org/docs.",
                                      "link_label": ["Dashboards", "", "Bad"],
                                      "link_url": ["https://example.org/grafana", "", "javascript:alert(1)"]})
    assert r.status_code == 400 and b"Each link needs" in r.data            # the javascript: link is refused
    r = b.post("/settings/branding", {"site_name": "OSG perfSONAR Meshes", "tagline": "WLCG and OSG",
                                      "accent": "#aa3300", "header": "#002244", "contact": "help@example.org",
                                      "about": "Run by the network team.\n\nDocs at https://example.org/docs. <script>x</script>",
                                      "link_label": ["Dashboards"], "link_url": ["https://example.org/grafana"]},
               follow_redirects=True)
    assert b"Branding saved" in r.data
    page = as_user("alice").get("/meshes").data.decode()
    assert "<title>Meshes · OSG perfSONAR Meshes</title>" in page and "WLCG and OSG" in page
    assert "--accent: #aa3300; --header: #002244;" in page
    assert '<a href="https://example.org/grafana" rel="noopener">Dashboards</a>' in page
    assert 'mailto:help@example.org' in page
    about = as_user("alice").get("/about").data.decode()
    assert "Run by the network team." in about and '<a href="https://example.org/docs" rel="noopener">' in about
    assert "<script>x</script>" not in about and "&lt;script&gt;" in about


def test_bad_colours_are_refused(app, as_user):
    r = as_user("admin").post("/settings/branding", {"accent": "red;} body{display:none", "header": "#002244"})
    assert r.status_code == 400 and b"Colours must look like" in r.data


def test_logo_upload_and_safe_serving(app, as_user):
    b = as_user("admin")
    r = b.post("/settings/branding", {"accent": "#0b5cab", "header": "#14283d",
                                      "logo": (io.BytesIO(PNG), "logo.png")}, content_type="multipart/form-data")
    assert r.status_code == 302
    r = as_user("alice").get("/branding/logo")
    assert r.status_code == 200 and r.mimetype == "image/png" and r.data == PNG
    assert "default-src 'none'" in r.headers["Content-Security-Policy"]
    assert b'src="/branding/logo"' in as_user("alice").get("/").data
    r = b.post("/settings/branding", {"accent": "#0b5cab", "header": "#14283d",
                                      "logo": (io.BytesIO(b"<html>not an image</html>"), "x.png")},
               content_type="multipart/form-data")
    assert r.status_code == 400 and b"must be a PNG" in r.data
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    b.post("/settings/branding", {"accent": "#0b5cab", "header": "#14283d", "logo": (io.BytesIO(svg), "l.svg")},
           content_type="multipart/form-data")
    r = b.get("/branding/logo")
    assert r.mimetype == "image/svg+xml" and "script-src" not in r.headers["Content-Security-Policy"]
    b.post("/settings/branding", {"action": "remove-logo"})
    assert b.get("/branding/logo").status_code == 404


def test_default_logo_and_favicon(app, as_user):
    page = as_user("alice").get("/").data.decode()
    assert 'src="/static/brand/pwa-next-icon.svg"' in page and 'href="/static/brand/favicon.svg"' in page
    assert "--accent: #1565c0; --header: #0b2a4a;" in page
    r = app.test_client().get("/favicon.ico")            # browsers ask before anyone signs in
    assert r.status_code == 200 and r.data[:4] == b"\x00\x00\x01\x00"
    assert app.test_client().get("/static/brand/horizontal-for-light.svg").status_code == 200
    about = as_user("alice").get("/about").data.decode()
    assert "brand/horizontal-for-light.svg" in about


def test_uploaded_logo_replaces_the_default(app, as_user):
    as_user("admin").post("/settings/branding", {"accent": "#1565c0", "header": "#0b2a4a",
                                                 "logo": (io.BytesIO(PNG), "logo.png")},
                          content_type="multipart/form-data")
    page = as_user("alice").get("/").data.decode()
    assert 'src="/branding/logo"' in page and "pwa-next-icon.svg" not in page
