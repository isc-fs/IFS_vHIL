"""Pass/skip/fail counts of a renode-test results directory (tooling/build.sh
writes renode-up-pkgs:/<name>.tests/): each NUnit project, and the Robot
suites (statistics of robot_output.xml), plus the failed and skipped tests.
    python3 summarize_tests.py <results dir> [...]"""
import glob
import os
import sys
import xml.etree.ElementTree as ET

for d in sys.argv[1:]:
    print(f"== {d}")
    for f in sorted(glob.glob(os.path.join(d, "results-*.xml"))):
        root = ET.parse(f).getroot()
        name = os.path.basename(f)[len("results-"):].split(".")[0]
        # NUnit 3 (test-run) or TRX
        if root.tag == "test-run":
            print(f"{name}: {root.get('passed')} passed, {root.get('skipped')} skipped, {root.get('failed')} failed")
        else:
            c = root.find(".//{*}Counters")
            print(f"{name}: {c.get('passed')} passed, {int(c.get('total')) - int(c.get('executed'))} not executed, {c.get('failed')} failed")
    out = os.path.join(d, "robot_output.xml")
    if os.path.exists(out):
        root = ET.parse(out).getroot()
        tests = root.findall(".//test")
        st = {}
        for t in tests:
            s = t.find("status").get("status")
            st.setdefault(s, []).append(t)
        suites = {s.get("name") for s in root.iter("suite") if s.find("test") is not None}
        print(f"Robot: {len(suites)} suites, " + ", ".join(f"{len(v)} {k.lower()}" for k, v in sorted(st.items())))
        for k in ("FAIL", "SKIP"):
            for t in st.get(k, []):
                print(f"  {k} {t.get('name')}")
