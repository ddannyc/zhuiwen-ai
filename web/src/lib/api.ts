// 单一接缝：全 app 只从这里取 api。后端就绪后改这一行指向 realApi。
import type { ChatApi } from "./contract";
import { mockApi } from "./mockApi";

export const api: ChatApi = mockApi;
