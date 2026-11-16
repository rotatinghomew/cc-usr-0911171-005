大型人才活动投递到就业意向归因图谱
测试：python3 -m unittest discover -s tests -v
构建：python3 -m compileall -q recruitment_atlas tests
验收：python3 -m recruitment_atlas --database /tmp/recruitment_atlas.sqlite3 run --fixture fixtures/acceptance.json --output /tmp/recruitment_atlas-output.json
单测：python3 -m unittest tests.test_domain -v
