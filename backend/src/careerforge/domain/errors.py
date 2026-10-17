class BusinessError(Exception):
    def __init__(self, status: int, code: str, message: str, **details: object) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        super().__init__(code)


def not_found() -> BusinessError:
    return BusinessError(404, "NOT_FOUND", "对象不存在或不可访问。")
