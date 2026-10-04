import io
import json

from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from typing import Any
from typing import override

from ..utils import as_json
from ..wire import GacCommand
from ..wire import RequestBase
from ..wire import ResponseBase
from ..wire import request
from ..wire import response


BROWSER_USE_MODULE_ID = 1


class PageStatus(Enum):
    Creating = 0
    Ready = 1
    Suspended = 2
    Closed = 3


@dataclass
class PageInfo:
    id: str = ''
    url: str = ''
    title: str = ''
    status: PageStatus = PageStatus.Creating

    def deserialize(self, des: ResponseBase, buf: io.BytesIO):
        page_id = des.read_str(buf)
        url = des.read_str(buf)
        title = des.read_str(buf)
        status_val = des.read_u8(buf)

        try:
            status = PageStatus(status_val)
        except ValueError as error:
            raise RuntimeError(
                f'invalid page status enum value: {status_val}'
            ) from error

        self.id = page_id
        self.url = url
        self.title = title
        self.status = status


@dataclass
class BrowserUseConsoleLog:
    type: str = 'log'
    text: str = ''

    def deserialize(self, des: ResponseBase, buf: io.BytesIO):
        self.type = des.read_str(buf)
        self.text = des.read_str(buf)


@dataclass
class BrowserUseNativeDialog:
    is_open: bool = False
    result: bool = False
    type: str = ''
    message: str = ''
    user_input: str = ''
    default_prompt: str = ''
    has_browser_handler: bool = False

    def deserialize(self, des: ResponseBase, buf: io.BytesIO):
        self.is_open = des.read_bool(buf)
        self.result = des.read_bool(buf)
        self.type = des.read_str(buf)
        self.message = des.read_str(buf)
        self.user_input = des.read_str(buf)
        self.default_prompt = des.read_str(buf)
        self.has_browser_handler = des.read_bool(buf)


@dataclass
class BrowserUseNetworkRequest:
    url: str = ''
    method: str = ''
    status: str = 'stale'
    create_time: float = 0.0
    update_time: float = 0.0

    def deserialize(self, des: ResponseBase, buf: io.BytesIO):
        self.url = des.read_str(buf)
        self.method = des.read_str(buf)
        self.status = des.read_str(buf)
        self.create_time = des.read_f64(buf)
        self.update_time = des.read_f64(buf)


class BrowserUseRequest:
    @request(module_id=BROWSER_USE_MODULE_ID, command=1)
    @dataclass
    class CreatePage(RequestBase):
        url: str
        title: str

        @override
        def serialize(self, out: io.BytesIO):
            self.write_str(out, self.url)
            self.write_str(out, self.title)

    @dataclass
    class RequestWithPageId(RequestBase):
        page_id: str

        @override
        def serialize(self, out: io.BytesIO):
            self.write_str(out, self.page_id)

    @request(module_id=BROWSER_USE_MODULE_ID, command=2)
    class ClosePage(RequestWithPageId):
        pass

    @request(module_id=BROWSER_USE_MODULE_ID, command=3)
    class GetPage(RequestWithPageId):
        pass

    @request(module_id=BROWSER_USE_MODULE_ID, command=4)
    @dataclass
    class ListPages(RequestBase):
        @override
        def serialize(self, _out: io.BytesIO):
            pass

    @request(module_id=BROWSER_USE_MODULE_ID, command=5)
    @dataclass
    class SendCdpCommand(RequestBase):
        page_id: str
        session_id: str
        method: str
        args: str

        @override
        def serialize(self, out: io.BytesIO):
            self.write_str(out, self.page_id)
            self.write_str(out, self.session_id)
            self.write_str(out, self.method)
            self.write_str(out, self.args)

    @request(module_id=BROWSER_USE_MODULE_ID, command=6)
    class GetPageDialogs(RequestWithPageId):
        pass

    @request(module_id=BROWSER_USE_MODULE_ID, command=7)
    class GetPageSize(RequestWithPageId):
        pass

    @request(module_id=BROWSER_USE_MODULE_ID, command=8)
    class DrainConsoleLogs(RequestWithPageId):
        pass

    @request(module_id=BROWSER_USE_MODULE_ID, command=9)
    class DrainNetworkRequests(RequestWithPageId):
        pass

    @request(module_id=BROWSER_USE_MODULE_ID, command=10)
    @dataclass
    class TakeNextDownload(RequestBase):
        @override
        def serialize(self, _out: io.BytesIO):
            pass

    @request(module_id=BROWSER_USE_MODULE_ID, command=11)
    @dataclass
    class StartDirectDownload(RequestBase):
        page_id: str
        url: str
        filename: str

        @override
        def serialize(self, out: io.BytesIO):
            self.write_str(out, self.page_id)
            self.write_str(out, self.url)
            self.write_str(out, self.filename)

    @request(module_id=BROWSER_USE_MODULE_ID, command=12)
    @dataclass
    class TakeDirectDownload(RequestBase):
        guid: str

        @override
        def serialize(self, out: io.BytesIO):
            self.write_str(out, self.guid)

    @request(module_id=BROWSER_USE_MODULE_ID, command=13)
    class ActivatePage(RequestWithPageId):
        pass

    @request(module_id=BROWSER_USE_MODULE_ID, command=14)
    @dataclass
    class CaptureScreenshot(RequestBase):
        page_id: str
        args: str
        max_dimension: int = 0

        @override
        def serialize(self, out: io.BytesIO):
            self.write_str(out, self.page_id)
            self.write_str(out, self.args)
            self.write_u32(out, self.max_dimension)

    @request(module_id=BROWSER_USE_MODULE_ID, command=16)
    @dataclass
    class GetActivePage(RequestBase):
        @override
        def serialize(self, _out: io.BytesIO):
            pass


class BrowserUseResponse:
    @response
    @dataclass
    class CreatePage(ResponseBase):
        page_id: str = ''

        @override
        def deserialize(self, buf: io.BytesIO):
            self.page_id = self.read_str(buf)

    @response
    @dataclass
    class ClosePage(ResponseBase):
        @override
        def deserialize(self, _buf: io.BytesIO):
            pass

    @response
    @dataclass
    class ActivatePage(ResponseBase):
        @override
        def deserialize(self, _buf: io.BytesIO):
            pass

    @response
    @dataclass
    class GetPage(ResponseBase):
        page: PageInfo = field(default_factory=PageInfo)

        @override
        def deserialize(self, buf: io.BytesIO):
            self.page.deserialize(self, buf)

    @response
    @dataclass
    class ListPages(ResponseBase):
        pages: list[PageInfo] = field(default_factory=list)

        @override
        def deserialize(self, buf: io.BytesIO):
            num_pages = self.read_u32(buf)
            self.pages = [PageInfo() for _ in range(num_pages)]

            for page in self.pages:
                page.deserialize(self, buf)

    @response
    @dataclass
    class SendCdpCommand(ResponseBase):
        output: str = ''

        @override
        def deserialize(self, buf: io.BytesIO):
            self.output = self.read_str(buf)

    @response
    @dataclass
    class CaptureScreenshot(ResponseBase):
        data: str = ''

        @override
        def deserialize(self, buf: io.BytesIO):
            self.data = self.read_str(buf)

    @response
    @dataclass
    class GetPageDialogs(ResponseBase):
        dialogs: dict[str, BrowserUseNativeDialog] = field(default_factory=dict)

        @override
        def deserialize(self, buf: io.BytesIO):
            num_dialogs = self.read_u32(buf)
            self.dialogs = {}

            for _ in range(num_dialogs):
                key = self.read_str(buf)
                dialog = BrowserUseNativeDialog()
                dialog.deserialize(self, buf)
                self.dialogs[key] = dialog

    @response
    @dataclass
    class GetPageSize(ResponseBase):
        width: int = 0
        height: int = 0

        @override
        def deserialize(self, buf: io.BytesIO):
            self.width = self.read_u32(buf)
            self.height = self.read_u32(buf)

    @response
    @dataclass
    class DrainConsoleLogs(ResponseBase):
        logs: list[BrowserUseConsoleLog] = field(default_factory=list)

        @override
        def deserialize(self, buf: io.BytesIO):
            num_logs = self.read_u32(buf)
            self.logs = [BrowserUseConsoleLog() for _ in range(num_logs)]

            for log in self.logs:
                log.deserialize(self, buf)

    @response
    @dataclass
    class DrainNetworkRequests(ResponseBase):
        requests: dict[str, BrowserUseNetworkRequest] = field(default_factory=dict)

        @override
        def deserialize(self, buf: io.BytesIO):
            num_requests = self.read_u32(buf)
            self.requests = {}

            for _ in range(num_requests):
                key = self.read_str(buf)
                req = BrowserUseNetworkRequest()
                req.deserialize(self, buf)
                self.requests[key] = req

    @response
    @dataclass
    class TakeNextDownload(ResponseBase):
        download: dict[str, Any] | None = None

        @override
        def deserialize(self, buf: io.BytesIO):
            if not self.read_bool(buf):
                self.download = None
                return
            self.download = {
                'guid': self.read_str(buf),
                'state': self.read_str(buf),
                'url': self.read_str(buf),
                'filename': self.read_str(buf),
                'path': self.read_str(buf),
                'bytes': self.read_u64(buf),
                'source': self.read_str(buf),
            }

    @response
    @dataclass
    class StartDirectDownload(ResponseBase):
        guid: str = ''

        @override
        def deserialize(self, buf: io.BytesIO):
            self.guid = self.read_str(buf)

    @response
    @dataclass
    class TakeDirectDownload(TakeNextDownload):
        pass

    @response
    @dataclass
    class GetActivePage(ResponseBase):
        page_id: str = ''

        @override
        def deserialize(self, buf: io.BytesIO):
            self.page_id = self.read_str(buf)


class BrowserUseCommand(GacCommand):
    @classmethod
    def create_page(cls, url: str, title: str) -> str:
        response = cls._execute(
            BrowserUseRequest.CreatePage(url, title),
            BrowserUseResponse.CreatePage,
        )
        return response.page_id

    @classmethod
    def close_page(cls, page_id: str):
        cls._execute(
            BrowserUseRequest.ClosePage(page_id),
            BrowserUseResponse.ClosePage,
        )

    @classmethod
    def activate_page(cls, page_id: str):
        cls._execute(
            BrowserUseRequest.ActivatePage(page_id),
            BrowserUseResponse.ActivatePage,
        )

    @classmethod
    def get_page(cls, page_id: str) -> PageInfo:
        response = cls._execute(
            BrowserUseRequest.GetPage(page_id),
            BrowserUseResponse.GetPage,
        )
        return response.page

    @classmethod
    def list_pages(cls) -> list[PageInfo]:
        response = cls._execute(
            BrowserUseRequest.ListPages(),
            BrowserUseResponse.ListPages,
        )
        return response.pages

    @classmethod
    def get_active_page(cls) -> str:
        response = cls._execute(
            BrowserUseRequest.GetActivePage(),
            BrowserUseResponse.GetActivePage,
        )
        return response.page_id

    @classmethod
    def send_cdp_command(
        cls,
        page_id: str,
        session_id: str,
        method: str,
        args: dict[str, Any],
    ) -> Any:
        response = cls._execute(
            BrowserUseRequest.SendCdpCommand(
                page_id,
                session_id,
                method,
                as_json(args) if args else '',
            ),
            BrowserUseResponse.SendCdpCommand,
        )
        try:
            return json.loads(response.output)
        except ValueError as error:
            raise RuntimeError('Cannot parse CDP command response') from error

    @classmethod
    def capture_screenshot(
        cls,
        page_id: str,
        args: dict[str, Any],
        max_dimension: int = 0,
    ) -> str:
        response = cls._execute(
            BrowserUseRequest.CaptureScreenshot(
                page_id,
                as_json(args) if args else '',
                max_dimension,
            ),
            BrowserUseResponse.CaptureScreenshot,
        )
        return response.data

    @classmethod
    def get_page_dialogs(
        cls,
        page_id: str,
    ) -> dict[str, BrowserUseNativeDialog]:
        response = cls._execute(
            BrowserUseRequest.GetPageDialogs(page_id),
            BrowserUseResponse.GetPageDialogs,
        )
        return response.dialogs

    @classmethod
    def get_page_size(cls, page_id: str) -> tuple[int, int]:
        response = cls._execute(
            BrowserUseRequest.GetPageSize(page_id),
            BrowserUseResponse.GetPageSize,
        )
        return response.width, response.height

    @classmethod
    def drain_console_logs(cls, page_id: str) -> list[BrowserUseConsoleLog]:
        response = cls._execute(
            BrowserUseRequest.DrainConsoleLogs(page_id),
            BrowserUseResponse.DrainConsoleLogs,
        )
        return response.logs

    @classmethod
    def drain_network_requests(
        cls,
        page_id: str,
    ) -> dict[str, BrowserUseNetworkRequest]:
        response = cls._execute(
            BrowserUseRequest.DrainNetworkRequests(page_id),
            BrowserUseResponse.DrainNetworkRequests,
        )
        return response.requests

    @classmethod
    def take_next_download(cls) -> dict[str, Any] | None:
        response = cls._execute(
            BrowserUseRequest.TakeNextDownload(),
            BrowserUseResponse.TakeNextDownload,
        )
        return response.download

    @classmethod
    def start_direct_download(
        cls,
        page_id: str,
        url: str,
        filename: str,
    ) -> str:
        response = cls._execute(
            BrowserUseRequest.StartDirectDownload(page_id, url, filename),
            BrowserUseResponse.StartDirectDownload,
        )
        return response.guid

    @classmethod
    def take_direct_download(cls, guid: str) -> dict[str, Any] | None:
        response = cls._execute(
            BrowserUseRequest.TakeDirectDownload(guid),
            BrowserUseResponse.TakeDirectDownload,
        )
        return response.download
