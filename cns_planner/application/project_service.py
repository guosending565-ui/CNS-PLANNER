"""Project metadata use cases."""


class ProjectService:
    def __init__(self, session, snapshot):
        self.session = session
        self.snapshot = snapshot

    def set_project(self, payload):
        name = str(payload.get("name", "")).strip()
        if not name or len(name) > 120:
            raise ValueError("项目名称须为 1–120 个字符")
        self.session.state["project"]["name"] = name
        self.session.save()
        return self.snapshot()
