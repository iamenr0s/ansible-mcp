def test_packages_installed(host):
    for pkg in ["curl", "git"]:
        assert host.package(pkg).is_installed

def test_service_running(host):
    svc = host.service("cron")
    assert svc.is_running
    assert svc.is_enabled
