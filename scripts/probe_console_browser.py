"""Small Edge CDP smoke check for the local Lab console."""

import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any
from uuid import UUID

import websockets
from websockets.typing import Origin

EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
PORT = 19328


async def main() -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")
    inspect_only = argparse.ArgumentParser()
    inspect_only.add_argument("--inspect-only", action="store_true")
    inspect_only.add_argument("--detector-only", action="store_true")
    inspect_only.add_argument("--late-compensation-required", action="store_true")
    inspect_only.add_argument("--probe-context-only", action="store_true")
    inspect_only.add_argument("--probe-timeout-required", action="store_true")
    inspect_only.add_argument("--runtime-request-required", action="store_true")
    inspect_only.add_argument("--database-context-required", action="store_true")
    inspect_only.add_argument("--request-link-required", action="store_true")
    inspect_only.add_argument("--source-membership-required", action="store_true")
    inspect_only.add_argument("--request-candidates-required", action="store_true")
    inspect_only.add_argument("--send-alert", action="store_true")
    inspect_only.add_argument("--width", type=int, default=390)
    inspect_only.add_argument("--height", type=int, default=844)
    inspect_only.add_argument("--screenshot", type=Path)
    inspect_only.add_argument("--shop-screenshot", type=Path)
    inspect_only.add_argument("--incident", type=UUID)
    inspect_only.add_argument("--advice-incident", type=UUID)
    inspect_only.add_argument("--overlap-incident", type=UUID)
    inspect_only.add_argument("--request-evidence-incident", type=UUID)
    args = inspect_only.parse_args()
    with tempfile.TemporaryDirectory(prefix="aegisops-edge-") as profile:
        process = subprocess.Popen(
            [
                str(EDGE),
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                f"--user-data-dir={profile}",
                f"--remote-debugging-port={PORT}",
                "--remote-allow-origins=*",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(50):
                try:
                    tabs = json.load(
                        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=1)
                    )
                    tab = next(tab for tab in tabs if tab.get("type") == "page")
                    break
                except (OSError, StopIteration):
                    await asyncio.sleep(0.2)
            else:
                raise RuntimeError("Edge CDP did not start")

            async with websockets.connect(
                tab["webSocketDebuggerUrl"], origin=Origin(f"http://127.0.0.1:{PORT}")
            ) as socket:
                serial = 0

                async def command(method: str, params: dict | None = None) -> dict:
                    nonlocal serial
                    serial += 1
                    current = serial
                    await socket.send(
                        json.dumps({"id": current, "method": method, "params": params or {}})
                    )
                    while True:
                        result = json.loads(await socket.recv())
                        if result.get("id") == current:
                            if "error" in result:
                                raise RuntimeError(result["error"])
                            return result.get("result", {})

                async def js(source: str) -> Any:
                    result = await command(
                        "Runtime.evaluate",
                        {"expression": source, "returnByValue": True, "awaitPromise": True},
                    )
                    if "exceptionDetails" in result:
                        raise RuntimeError(result["exceptionDetails"])
                    return result.get("result", {}).get("value")

                await command("Page.enable")
                await command(
                    "Emulation.setDeviceMetricsOverride",
                    {
                        "width": args.width,
                        "height": args.height,
                        "deviceScaleFactor": 1,
                        "mobile": args.width <= 600,
                    },
                )
                target = os.getenv("AEGISOPS_CONSOLE_URL", "http://127.0.0.1:8010") + "/"
                if args.incident:
                    target += f"?view=ops&incident={args.incident}"
                await command("Page.navigate", {"url": target})
                for _ in range(50):
                    if await js("document.querySelectorAll('.product-card').length") == 4:
                        break
                    await asyncio.sleep(0.2)
                print(
                    "mobile",
                    json.dumps(
                        await js("""({
                    viewport: innerWidth, document: document.documentElement.scrollWidth,
                    panel: document.querySelector('.catalog-panel')
                        .getBoundingClientRect().toJSON(),
                    card: document.querySelector('.product-card')?.getBoundingClientRect().toJSON(),
                    products: document.querySelectorAll('.product-card').length
                })"""),
                        ensure_ascii=False,
                    ),
                )
                if args.shop_screenshot:
                    capture = await command("Page.captureScreenshot", {"format": "png"})
                    args.shop_screenshot.parent.mkdir(parents=True, exist_ok=True)
                    args.shop_screenshot.write_bytes(base64.b64decode(capture["data"]))
                    print("shop screenshot", args.shop_screenshot.resolve())
                if not args.inspect_only and not args.send_alert:
                    await js("document.querySelectorAll('.product-card')[1].click()")
                    await js("document.querySelector('#qty-plus').click()")
                    print(
                        "selection",
                        json.dumps(
                            await js("""({
                        product: document.querySelector('#selected-product').textContent,
                        quantity: document.querySelector('#quantity').value,
                        total: document.querySelector('#order-total').textContent
                    })"""),
                            ensure_ascii=False,
                        ),
                    )
                    await js("document.querySelector('#checkout-button').click()")
                    for _ in range(60):
                        if await js(
                            "document.querySelector('#checkout-result').textContent.includes('订单号：')"
                        ):
                            break
                        await asyncio.sleep(0.2)
                    print(
                        "checkout",
                        json.dumps(
                            await js("""({
                        result: document.querySelector('#checkout-result').textContent,
                        error: document.querySelector('#checkout-result')
                            .classList.contains('error')
                    })"""),
                            ensure_ascii=False,
                        ),
                    )
                await js("document.querySelector('#open-ops').click()")
                for _ in range(60):
                    if await js("document.querySelectorAll('.scenario-item').length") == 3:
                        break
                    await asyncio.sleep(0.2)
                print(
                    "ops",
                    json.dumps(
                        await js("""({
                    visible: !document.querySelector('#ops-view').classList.contains('hidden'),
                    scenarios: document.querySelectorAll('.scenario-item').length,
                    incidents: document.querySelectorAll('.incident-row').length,
                    customAlert: !!document.querySelector('#lab-alert-form'),
                    detector: document.querySelector('#detector-summary')?.textContent,
                    document: document.documentElement.scrollWidth
                })"""),
                        ensure_ascii=False,
                    ),
                )
                if args.detector_only:
                    summary = ""
                    for _ in range(50):
                        summary = await js(
                            "document.querySelector('#detector-summary')?.textContent ?? ''")
                        if "定稿" in summary:
                            break
                        await asyncio.sleep(0.2)
                    if "定稿" not in summary:
                        raise AssertionError(f"detector status did not render: {summary}")
                    if args.late_compensation_required:
                        await js("document.querySelector('#request-signal-list')"
                                 ".closest('details').open = true")
                        detail = await js("""({
                            late: document.querySelector('#detector-detail').textContent,
                            signals: document.querySelector('#request-signal-list').textContent,
                            viewport: innerWidth, width: document.documentElement.scrollWidth
                        })""")
                        if ("迟到补算" not in detail["late"]
                                or "迟到事件补算" not in detail["signals"]
                                or detail["width"] > detail["viewport"]):
                            raise AssertionError("late compensation or responsive UI missing")
                        print("late_compensation", json.dumps(detail, ensure_ascii=False))
                    if args.screenshot:
                        # The incident detail loads asynchronously; wait for the
                        # investigation block before capturing the acceptance image.
                        for _ in range(50):
                            detail_text = await js(
                                "document.querySelector('#incident-detail')?.textContent ?? ''")
                            if "调查结论" in detail_text and "正在读取" not in detail_text:
                                break
                            await asyncio.sleep(0.2)
                        capture = await command("Page.captureScreenshot", {"format": "png"})
                        args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                        args.screenshot.write_bytes(base64.b64decode(capture["data"]))
                        print("ops screenshot", args.screenshot.resolve())
                    gates = await js("""Array.from(
                        document.querySelectorAll('#incident-detail p'))
                        .map(p => p.textContent).filter(t => t.includes('根因门禁'))""")
                    overflow = await js(
                        "document.documentElement.scrollWidth > innerWidth")
                    print("root_cause_gates", json.dumps(
                        {"lines": gates, "horizontal_overflow": overflow,
                         "viewport": args.width}, ensure_ascii=False))
                    if not gates:
                        raise AssertionError("root cause gate lines did not render")
                    return
                for _ in range(100):
                    if await js("document.querySelector('.case-intro h3') !== null"):
                        break
                    await asyncio.sleep(0.2)
                print(
                    "default",
                    json.dumps(
                        await js("""({
                    title: document.querySelector('.case-intro h3')?.textContent,
                    incident: new URLSearchParams(location.search).get('incident')
                })"""),
                        ensure_ascii=False,
                    ),
                )
                if not args.request_evidence_incident:
                    await js("""(() => { const field = document.querySelector('#incident-filter');
                        field.value = state.overview.incidents[0]?.title || '';
                        field.dispatchEvent(new Event('input')); })()""")
                    search_count = await js("document.querySelectorAll('.incident-row').length")
                    if search_count < 1:
                        raise AssertionError(
                            f"expected a matching existing incident, found {search_count}"
                        )
                    print("search", search_count)
                await js("""(() => { const field = document.querySelector('#incident-filter');
                    field.value = ''; field.dispatchEvent(new Event('input'));
                    document.querySelector('#lab-tools').open = true; })()""")
                print("tools_open", await js("document.querySelector('#lab-tools').open"))
                if args.send_alert:
                    await js("""document.querySelector('#lab-alert-form [name=alert_name]').value =
                        'UnmappedLatencySpike';
                        document.querySelector('#lab-alert-form').requestSubmit()""")
                    for _ in range(60):
                        if await js(
                            "document.querySelector('#lab-alert-result').textContent.includes('已接收')"
                        ):
                            break
                        await asyncio.sleep(0.2)
                    print(
                        "alert",
                        json.dumps(
                            await js("document.querySelector('#lab-alert-result').textContent"),
                            ensure_ascii=False,
                        ),
                    )
                if args.inspect_only:
                    selected_incident = (
                        args.request_evidence_incident
                        or args.overlap_incident
                        or args.advice_incident
                    )
                    if selected_incident:
                        await js(f"selectIncident({json.dumps(str(selected_incident))})")
                    else:
                        await js("document.querySelector('.incident-row').click()")
                    for _ in range(100):
                        if await js("document.querySelector('.remediation-panel') !== null"):
                            break
                        await asyncio.sleep(0.2)
                    detail = await js("""({
                        text: document.querySelector('.remediation-panel')?.textContent,
                        limitations: document.querySelector('#incident-detail')
                            ?.textContent.includes('调查限制')
                    })""")
                    if args.advice_incident and (
                        not detail["limitations"] or "没有可执行" not in detail["text"]
                    ):
                        raise AssertionError("unknown incident does not show its execution limit")
                    print("detail", json.dumps(detail, ensure_ascii=False))
                    if args.probe_context_only:
                        if args.request_candidates_required:
                            await js("""(() => {
                                const section = Array.from(document.querySelectorAll(
                                    '#incident-detail h3')).find(item => item.textContent
                                    .includes('来源绑定的请求关系'))?.parentElement;
                                section?.querySelectorAll('details').forEach(d => {
                                    if (!d.parentElement.closest('details')) d.open = true;
                                });
                                section?.scrollIntoView({block: 'start'});
                            })()""")
                            candidate_context = await js("""({
                                title: document.querySelector('#incident-detail').textContent
                                    .includes('来源绑定的请求关系'),
                                coverage: document.querySelector('#incident-detail').textContent
                                    .includes('条已核验追踪 · 覆盖'),
                                conflict: document.querySelector('#incident-detail').textContent
                                    .includes('同方向存在多个来源组'),
                                limit: document.querySelector('#incident-detail').textContent
                                    .includes('不授权自动合并或修复'),
                                noOverflow: document.documentElement.scrollWidth <= innerWidth
                            })""")
                            if not all(candidate_context.values()):
                                raise AssertionError(f"candidate rendering: {candidate_context}")
                            print("request_candidates", json.dumps(candidate_context), flush=True)
                        context = await js("""({
                            visible: document.querySelector('#incident-detail')
                                .textContent.includes('端点探针上下文'),
                            overflow: document.documentElement.scrollWidth > innerWidth,
                            timeout: document.querySelector('#incident-detail')
                                .textContent.includes('响应超时')
                        })""")
                        if args.database_context_required:
                            database_context = await js("""({
                                fact: document.querySelector('#incident-detail')
                                    .textContent.includes('已核验数据库操作'),
                                lock: document.querySelector('#incident-detail')
                                    .textContent.includes('锁等待超时'),
                                sqlstate: document.querySelector('#incident-detail')
                                    .textContent.includes('SQLSTATE 55P03'),
                                unknown: document.querySelector('#incident-detail')
                                    .textContent.includes('当前状态未知'),
                                advice: document.querySelector('#incident-detail')
                                    .textContent.includes('核对数据库锁等待'),
                                path: document.querySelector('#incident-detail')
                                    .textContent.includes('已核验调用路径')
                            })""")
                            if not all(database_context.values()):
                                raise AssertionError(
                                    f"missing database context: {database_context}")
                            print("database_context", json.dumps(database_context), flush=True)
                        if args.runtime_request_required:
                            runtime_context = await js("""({
                                runtime: document.querySelector('#incident-detail')
                                    .textContent.includes('运行时状态观测'),
                                request: document.querySelector('#incident-detail')
                                    .textContent.includes('已核验依赖请求'),
                                unbound: document.querySelector('#incident-detail')
                                    .textContent.includes('未绑定聚合告警')
                            })""")
                            if not all(runtime_context.values()):
                                raise AssertionError(
                                    f"missing runtime/request facts: {runtime_context}",
                                )
                            print("runtime_request", json.dumps(runtime_context), flush=True)
                        if args.request_link_required:
                            link_context = await js("""({
                                link: document.querySelector('#incident-detail')
                                    .textContent.includes('已核验请求上下游关系'),
                                unbound: document.querySelector('#incident-detail')
                                    .textContent.includes('未绑定聚合告警'),
                                root: document.querySelector('#incident-detail')
                                    .textContent.includes('根因未确定')
                            })""")
                            if not all(link_context.values()):
                                raise AssertionError(f"missing request links: {link_context}")
                            print("request_links", json.dumps(link_context), flush=True)
                        if args.source_membership_required:
                            source_context = await js("""({
                                member: document.querySelector('#incident-detail')
                                    .textContent.includes('已核验告警来源成员'),
                                uncertain: document.querySelector('#incident-detail')
                                    .textContent.includes('当前故障状态未知'),
                                form: !!document.querySelector('#request-signal-list form')
                            })""")
                            if not all(source_context.values()):
                                raise AssertionError(f"missing source membership: {source_context}")
                            await js("""(() => {
                                const form = document.querySelector('#request-signal-list form');
                                form.closest('details').open = true;
                                form.querySelector('input').value = '浏览器核对已有请求症状来源';
                                form.querySelector('button').click();
                            })()""")
                            promoted = False
                            for _ in range(60):
                                promoted = await js("""document.querySelector(
                                    '#request-signal-list form [role=status]')
                                    .textContent.includes('已送入调查')""")
                                if promoted:
                                    break
                                await asyncio.sleep(0.2)
                            if not promoted:
                                raise AssertionError("source promotion form did not succeed")
                            if await js("document.documentElement.scrollWidth > innerWidth"):
                                raise AssertionError("expanded source form causes overflow")
                            print("source_membership", json.dumps(source_context), flush=True)
                        if args.probe_timeout_required and not context["timeout"]:
                            raise AssertionError("probe timeout category is missing in Chinese")
                        if ((not context["visible"] and not args.request_link_required
                             and not args.source_membership_required
                             and not args.request_candidates_required)
                                or context["overflow"]):
                            raise AssertionError(f"probe context rendering failed: {context}")
                        if args.screenshot:
                            if args.database_context_required:
                                await js("""Array.from(document.querySelectorAll(
                                    '#incident-detail li')).find(item => item.textContent
                                    .includes('已核验数据库操作'))
                                    ?.scrollIntoView({block: 'center'})""")
                                await asyncio.sleep(0.3)
                            if args.source_membership_required:
                                await js("""Array.from(document.querySelectorAll(
                                    '#incident-detail li')).find(item => item.textContent
                                    .includes('已核验告警来源成员'))
                                    ?.scrollIntoView({block: 'center'})""")
                                await asyncio.sleep(0.3)
                            if args.request_link_required:
                                await js("""Array.from(document.querySelectorAll(
                                    '#incident-detail li')).find(item => item.textContent
                                    .includes('已核验请求上下游关系'))
                                    ?.scrollIntoView({block: 'center'})""")
                                await asyncio.sleep(0.3)
                            capture = await command("Page.captureScreenshot", {"format": "png"})
                            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                            args.screenshot.write_bytes(base64.b64decode(capture["data"]))
                        print("probe_context", json.dumps(context), flush=True)
                        return
                    if args.request_evidence_incident:
                        evidence = await js("""({
                            verified: document.querySelector('#incident-detail')
                                .textContent.includes('Span 事实已核验'),
                            event: document.querySelector('#incident-detail')
                                .textContent.includes('dependency_transport'),
                            form: !!document.querySelector('.request-evidence-form'),
                            overflow: document.documentElement.scrollWidth > innerWidth
                        })""")
                        if not (
                            evidence["verified"] and evidence["event"] and evidence["form"]
                        ):
                            raise AssertionError("verified request evidence is not visible")
                        if evidence["overflow"]:
                            raise AssertionError("request evidence causes horizontal overflow")
                        print("request_evidence", json.dumps(evidence, ensure_ascii=False))
                        await js("""document.querySelector('.request-evidence-form')
                            .scrollIntoView({block: 'center'})""")
                        await asyncio.sleep(0.7)
                        print("evidence_viewport", await js("""({
                            scrollY, top: document.querySelector('.request-evidence-form')
                                .getBoundingClientRect().top
                        })"""))
                    if args.overlap_incident:
                        overlap_links = await js("""Array.from(document.querySelectorAll(
                            '#incident-detail a')).filter(link =>
                            link.textContent.includes('查看其他报告')).map(link => link.href)""")
                        if not overlap_links:
                            raise AssertionError("trace overlap observation is not visible")
                        disclaimer = await js("""document.querySelector('#incident-detail')
                            .textContent.includes('未证明告警共享请求、事故相关或根因相同')""")
                        if not disclaimer:
                            raise AssertionError("trace overlap lacks causality disclaimer")
                        print("trace_overlaps", json.dumps(overlap_links, ensure_ascii=False))
                    if not args.request_evidence_incident:
                        await js("""selectIncident(state.overview.incidents.find(
                            incident => incident.title.includes('faulty order deployment')
                        ).id)""")
                        known = await js("""({
                            text: document.querySelector('.remediation-panel')?.textContent,
                            detail: document.querySelector('#incident-detail')?.textContent
                        })""")
                        if (
                            "回退订单服务" not in known["text"]
                            or "现有证据支持订单服务错误部署" not in known["detail"]
                        ):
                            raise AssertionError("historical English advice was not localized")
                        print("known", json.dumps(known, ensure_ascii=False))
                    if args.advice_incident:
                        await js(f"selectIncident({json.dumps(str(args.advice_incident))})")
                        advice_count = await js(
                            "document.querySelectorAll('.advice-list li').length"
                        )
                        if advice_count < 1:
                            raise AssertionError("unknown incident has no visible operator advice")
                        print("advice", advice_count)
                    if args.screenshot:
                        capture_params: dict = {"format": "png"}
                        if args.request_evidence_incident:
                            clip_y = await js("""Math.max(0, document.querySelector(
                                '.request-evidence-form').getBoundingClientRect().top
                                + window.scrollY - 220)""")
                            capture_params.update({
                                "captureBeyondViewport": True,
                                "clip": {"x": 0, "y": clip_y, "width": args.width,
                                         "height": args.height, "scale": 1},
                            })
                        capture = await command("Page.captureScreenshot", capture_params)
                        args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                        args.screenshot.write_bytes(base64.b64decode(capture["data"]))
                        print("screenshot", args.screenshot.resolve())
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    asyncio.run(main())
