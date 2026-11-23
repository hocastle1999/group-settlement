import os

from hypothesis import settings

# CI에서는 무작위 시드를 고정해(derandomize) 같은 커밋이면 항상 같은 입력으로 돌게 한다.
# 로컬에서는 매번 새 입력을 시도해 반례를 더 넓게 찾는다.
settings.register_profile("ci", derandomize=True, max_examples=300)
settings.register_profile("dev", max_examples=200)
settings.load_profile("ci" if os.environ.get("CI") else "dev")
