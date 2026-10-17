import { beforeEach, describe, expect, it } from "vitest";

import { clearPrefill, readPrefill, setPrefill } from "./prefill";

const prefill = {
  frameworkId: "tpl-thermos-cup",
  title: "周五晚场（重新分析）",
  file: { path: "C:\\data\\recordings\\abc.wav", name: "abc.wav" },
  windowSeconds: 60,
  intervalSeconds: 30,
};

describe("开播预填", () => {
  beforeEach(() => sessionStorage.clear());

  it("读取不删除，清除后读不到", () => {
    setPrefill(prefill);
    expect(readPrefill()).toEqual(prefill);
    expect(readPrefill()).toEqual(prefill); // 开发模式下初始化会执行两次
    clearPrefill();
    expect(readPrefill()).toBeNull();
  });

  it("内容损坏或缺少文件时当作没有预填", () => {
    sessionStorage.setItem("monitor-prefill", "{oops");
    expect(readPrefill()).toBeNull();
    sessionStorage.setItem("monitor-prefill", JSON.stringify({ frameworkId: "x", title: "" }));
    expect(readPrefill()).toBeNull();
  });
});
