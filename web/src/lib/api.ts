// 单一接缝：全 app 只从这里取 api。打真后端（FastAPI /auth、/chat）。
import type { ChatApi } from "./contract";
import { realApi } from "./realApi";

export const api: ChatApi = realApi;
