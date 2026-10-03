"""固件镜像管理与分批灰度升级/回滚。

批次状态机：
    PENDING --升级开始--> IN_PROGRESS --全部健康--> SUCCEEDED
                                  |
                                  +--任一设备异常/健康检查失败/人工回滚--> ROLLED_BACK

规则：
- 批次严格按 order 推进：前一批次未 SUCCEEDED，后续批次不得开始；
- 批次内任一设备失败，整批回滚到各设备升级前的版本，其他批次不受影响；
- 设备 firmware_version 只在批次成功时才推进，回滚恢复 from_version；
- 所有“检查 + 变更 + 快照”在同一把 Store 锁内完成，并发请求下
  重复开始会以 batch_in_progress / batch_already_* 拒绝，进度互不覆盖。
"""
from __future__ import annotations

import hashlib
import logging
import time
import uuid
from typing import Callable

from app.errors import ConflictError, NotFoundError
from app.models import (
    Batch,
    BatchState,
    Campaign,
    CampaignState,
    DeviceOccupation,
    DeviceUpgrade,
    DeviceUpgradeState,
    FirmwareImage,
    UpgradeEvent,
    UpgradeEventType,
)
from app.store import Store

logger = logging.getLogger("iot.upgrade")


class UpgradeService:
    def __init__(self, store: Store, clock: Callable[[], float] | None = None):
        self.store = store
        self.clock = clock or time.time

    # ---- 固件镜像（本地模拟） --------------------------------------------
    def release_firmware(self, version: str, size: int = 1024) -> FirmwareImage:
        with self.store.lock():
            if self.store.get_firmware(version) is not None:
                raise ConflictError("firmware_exists",
                                    f"固件 {version} 已存在")
            checksum = hashlib.sha256(
                f"local-firmware:{version}".encode()).hexdigest()
            fw = FirmwareImage(version=version, checksum=checksum,
                               size=size, created_at=self.clock(),
                               released=True)
            self.store.put_firmware(fw)
            self.store.save()
            logger.info("firmware released version=%s checksum=%s",
                        version, checksum[:12])
            return fw

    # ---- 升级活动 --------------------------------------------------------
    def create_campaign(self, firmware_version: str, device_ids: list[str],
                        batch_size: int = 2) -> Campaign:
        with self.store.lock():
            fw = self.store.get_firmware(firmware_version)
            if fw is None or not fw.released:
                raise NotFoundError("firmware_not_found",
                                    f"固件 {firmware_version} 未发布")
            if not device_ids:
                raise ConflictError("empty_campaign", "升级设备列表为空")
            for device_id in device_ids:
                device = self.store.get_device(device_id)
                if device is None:
                    raise NotFoundError(
                        "device_not_registered", f"设备 {device_id} 未注册")
                if device.upgrade_state == DeviceUpgradeState.UPGRADING:
                    raise ConflictError(
                        "device_upgrading",
                        f"设备 {device_id} 正在其他升级中")
                if device.firmware_version == firmware_version:
                    raise ConflictError(
                        "device_already_on_target",
                        f"设备 {device_id} 已在版本 {firmware_version}")

            campaign_id = f"camp-{uuid.uuid4().hex[:12]}"
            batches: list[Batch] = []
            for i in range(0, len(device_ids), max(1, batch_size)):
                order = i // max(1, batch_size)
                chunk = device_ids[i:i + max(1, batch_size)]
                batches.append(Batch(
                    batch_id=f"{campaign_id}-b{order}",
                    campaign_id=campaign_id,
                    device_ids=list(chunk),
                    order=order,
                ))
            campaign = Campaign(campaign_id=campaign_id,
                                firmware_version=firmware_version,
                                batches=batches,
                                created_at=self.clock())
            self.store.put_campaign(campaign)
            self.store.save()
            logger.info(
                "campaign created id=%s version=%s devices=%d batches=%d",
                campaign_id, firmware_version, len(device_ids), len(batches))
            return campaign

    def _require_campaign(self, campaign_id: str) -> Campaign:
        campaign = self.store.get_campaign(campaign_id)
        if campaign is None:
            raise NotFoundError("campaign_not_found",
                                f"升级活动 {campaign_id} 不存在")
        return campaign

    def _find(self, campaign_id: str, batch_id: str
              ) -> tuple[Campaign, Batch]:
        campaign = self.store.get_campaign(campaign_id)
        if campaign is None:
            raise NotFoundError("campaign_not_found",
                                f"升级活动 {campaign_id} 不存在")
        batch = next((b for b in campaign.batches
                      if b.batch_id == batch_id), None)
        if batch is None:
            raise NotFoundError("batch_not_found",
                                f"批次 {batch_id} 不存在")
        return campaign, batch

    def upgrade_batch(self, campaign_id: str, batch_id: str,
                      health_results: dict[str, bool] | None = None) -> dict:
        with self.store.lock():
            campaign, batch = self._find(campaign_id, batch_id)

            if batch.state == BatchState.SUCCEEDED:
                raise ConflictError("batch_already_succeeded",
                                    f"批次 {batch_id} 已成功，勿重复推进")
            if batch.state == BatchState.ROLLED_BACK:
                raise ConflictError("batch_rolled_back",
                                    f"批次 {batch_id} 已回滚，终止态")
            if batch.state == BatchState.IN_PROGRESS:
                raise ConflictError("batch_in_progress",
                                    f"批次 {batch_id} 正在升级中")
            if batch.state == BatchState.CANCELLED:
                raise ConflictError("batch_cancelled",
                                    f"批次 {batch_id} 已随活动取消")
            # 严格按批次顺序推进
            for prev in campaign.batches:
                if prev.order < batch.order and prev.state != BatchState.SUCCEEDED:
                    raise ConflictError(
                        "previous_batch_not_succeeded",
                        f"前序批次 {prev.batch_id} 状态为 {prev.state.value}")
            # 活动生命周期：暂停后不能开启新批次，取消后一切推进停止
            if campaign.state == CampaignState.PAUSED:
                raise ConflictError(
                    "campaign_paused",
                    f"活动 {campaign_id} 已暂停，不能推进新批次")
            if campaign.state == CampaignState.CANCELLED:
                raise ConflictError(
                    "campaign_cancelled",
                    f"活动 {campaign_id} 已取消，不能推进批次")

            # 设备占用：本批次所有设备必须未被其他进行中的活动占着。
            # 检查与占用在同一把锁内原子完成，并发争抢只有一方能成功，
            # 失败方立即被拒（独立原因码），不改动任何设备。
            for device_id in batch.device_ids:
                occupation = self.store.get_occupation(device_id)
                if occupation is not None \
                        and occupation.campaign_id != campaign_id:
                    raise ConflictError(
                        "device_occupied_by_other_campaign",
                        f"设备 {device_id} 正被活动 "
                        f"{occupation.campaign_id} 占用")

            target = campaign.firmware_version
            now = self.clock()
            # 进入 IN_PROGRESS 并先落盘（中途失败重启也能看到一致中间态）
            for device_id in batch.device_ids:
                device = self.store.get_device(device_id)
                from_version = device.firmware_version
                device.upgrade_state = DeviceUpgradeState.UPGRADING
                device.target_version = target
                device.campaign_id = campaign_id
                self.store.put_device(device)
                self.store.put_occupation(DeviceOccupation(
                    device_id=device_id, campaign_id=campaign_id,
                    batch_id=batch_id, acquired_at=now))
                batch.upgrades[device_id] = DeviceUpgrade(
                    device_id=device_id,
                    from_version=from_version,
                    to_version=target,
                    state=DeviceUpgradeState.UPGRADING,
                )
                self._append_event(
                    device_id, campaign_id, batch_id,
                    UpgradeEventType.STARTED,
                    from_version, target, "批次开始升级")
            batch.state = BatchState.IN_PROGRESS
            self.store.put_campaign(campaign)
            self.store.save()
            logger.info("batch started campaign=%s batch=%s devices=%s",
                        campaign_id, batch_id, batch.device_ids)

            # health_results=None：批次停留在 IN_PROGRESS，等待设备逐个
            # report_device 上报；显式传入 dict（含 {}）则立即据结果终结，
            # 未给出结果的设备视为健康。
            if health_results is None:
                return batch.to_dict()
            for device_id, healthy in health_results.items():
                if device_id in batch.upgrades:
                    batch.upgrades[device_id].healthy = bool(healthy)
            for up in batch.upgrades.values():
                if up.healthy is None:
                    up.healthy = True
            return self._finalize(campaign, batch)

    def report_device(self, campaign_id: str, batch_id: str,
                      device_id: str, ok: bool, message: str = "") -> dict:
        """批次进行中设备上报：异常即触发整批回滚。"""
        with self.store.lock():
            campaign, batch = self._find(campaign_id, batch_id)
            if batch.state != BatchState.IN_PROGRESS:
                raise ConflictError(
                    "batch_not_in_progress",
                    f"批次 {batch_id} 当前状态 {batch.state.value}，不可上报")
            up = batch.upgrades.get(device_id)
            if up is None:
                raise NotFoundError("device_not_in_batch",
                                    f"设备 {device_id} 不在批次 {batch_id}")
            up.healthy = bool(ok)
            up.message = message
            logger.info(
                "device reported campaign=%s batch=%s device=%s ok=%s msg=%s",
                campaign_id, batch_id, device_id, ok, message)
            if not ok:
                return self._finalize(campaign, batch)
            if all(u.healthy is True for u in batch.upgrades.values()):
                return self._finalize(campaign, batch)
            self.store.put_campaign(campaign)
            self.store.save()
            return batch.to_dict()

    def rollback_batch(self, campaign_id: str, batch_id: str) -> dict:
        with self.store.lock():
            campaign, batch = self._find(campaign_id, batch_id)
            if batch.state == BatchState.ROLLED_BACK:
                return batch.to_dict()
            if batch.state != BatchState.IN_PROGRESS:
                raise ConflictError(
                    "batch_not_in_progress",
                    f"批次 {batch_id} 未在升级中（{batch.state.value}），无需回滚")
            for up in batch.upgrades.values():
                up.healthy = False
                if not up.message:
                    up.message = "人工触发整批回滚"
            return self._finalize(campaign, batch)

    def _finalize(self, campaign: Campaign, batch: Batch) -> dict:
        failed = [u for u in batch.upgrades.values() if u.healthy is False]
        if failed:
            # 整批回滚：每个设备恢复各自的 from_version，其他批次不动
            for up in batch.upgrades.values():
                device = self.store.get_device(up.device_id)
                device.firmware_version = up.from_version
                device.campaign_id = campaign.campaign_id
                device.target_version = up.to_version
                if up.healthy is False:
                    up.state = DeviceUpgradeState.FAILED
                    device.upgrade_state = DeviceUpgradeState.FAILED
                    if not up.message:
                        up.message = "健康检查失败，随批次回滚"
                else:
                    up.state = DeviceUpgradeState.ROLLED_BACK
                    device.upgrade_state = DeviceUpgradeState.ROLLED_BACK
                    up.message = up.message or "同批存在失败设备，整批回滚"
                self.store.put_device(device)
                # 终态释放占用，并记录本次回滚往来。
                # 回滚是把设备从目标版本拉回原版本：事件的 from/to 必须
                # 反映真实落点（to_version=设备回退后所在版本），这样
                # 往来链条按顺序追下去才能落到设备当前版本。
                self.store.delete_occupation(up.device_id)
                self._append_event(
                    up.device_id, campaign.campaign_id, batch.batch_id,
                    UpgradeEventType.ROLLED_BACK,
                    up.to_version, up.from_version, up.message)
            batch.state = BatchState.ROLLED_BACK
            logger.info(
                "batch rolled back campaign=%s batch=%s failed_devices=%s",
                campaign.campaign_id, batch.batch_id,
                [u.device_id for u in failed])
        else:
            for up in batch.upgrades.values():
                device = self.store.get_device(up.device_id)
                # 仅在批次整体成功时推进设备当前版本
                device.firmware_version = up.to_version
                device.target_version = up.to_version
                device.campaign_id = campaign.campaign_id
                device.upgrade_state = DeviceUpgradeState.ON_TARGET
                up.state = DeviceUpgradeState.ON_TARGET
                up.healthy = True
                self.store.put_device(device)
                # 终态释放占用，并记录本次成功往来
                self.store.delete_occupation(up.device_id)
                self._append_event(
                    up.device_id, campaign.campaign_id, batch.batch_id,
                    UpgradeEventType.SUCCEEDED,
                    up.from_version, up.to_version, "升级成功")
            batch.state = BatchState.SUCCEEDED
            logger.info("batch succeeded campaign=%s batch=%s",
                        campaign.campaign_id, batch.batch_id)
        self.store.put_campaign(campaign)
        self.store.save()
        return batch.to_dict()

    def _append_event(self, device_id: str, campaign_id: str, batch_id: str,
                      event_type: UpgradeEventType, from_version: str,
                      to_version: str, message: str = "") -> None:
        """追加单台设备升级往来。调用方须已持有 Store 锁。"""
        seq = self.store.next_event_seq(device_id)
        self.store.append_upgrade_event(UpgradeEvent(
            seq=seq, device_id=device_id, campaign_id=campaign_id,
            batch_id=batch_id, event_type=event_type,
            from_version=from_version, to_version=to_version,
            at=self.clock(), message=message))

    # ---- 重启 / 原地升级后的占用恢复 --------------------------------------
    def restore_occupations(self) -> dict:
        """从批次状态重建设备占用关系（幂等，每次启动调用一次）。

        上一版快照里没有 device_occupations：原地升级后，仍在
        IN_PROGRESS 批次名下的设备必须重新被对应活动/批次占着；
        反过来，指向已终态（succeeded/rolled_back/cancelled）批次或
        已不存在批次的残留占用一律释放。只有发生变更才落盘。
        """
        with self.store.lock():
            # 期望占用：device_id -> (campaign_id, batch_id)，
            # 来源是所有活动的进行中批次（先到先占，冲突记告警）。
            expected: dict[str, tuple[str, str]] = {}
            for campaign in self.store.list_campaigns():
                for batch in campaign.batches:
                    if batch.state != BatchState.IN_PROGRESS:
                        continue
                    for device_id in batch.device_ids:
                        claim = (campaign.campaign_id, batch.batch_id)
                        prev = expected.get(device_id)
                        if prev is not None and prev != claim:
                            logger.warning(
                                "occupation conflict on restore device=%s "
                                "keep=%s/%s drop=%s/%s",
                                device_id, prev[0], prev[1],
                                claim[0], claim[1])
                            continue
                        expected[device_id] = claim

            rebuilt = released = 0
            # 释放残留：占用指向的批次已终态或不存在
            for occ in self.store.list_occupations():
                want = expected.get(occ.device_id)
                if want is None or want != (occ.campaign_id, occ.batch_id):
                    self.store.delete_occupation(occ.device_id)
                    released += 1
                    logger.info(
                        "occupation released on restore device=%s "
                        "stale=%s/%s", occ.device_id,
                        occ.campaign_id, occ.batch_id)
            # 重建缺失/纠正错配
            for device_id, (campaign_id, batch_id) in expected.items():
                occ = self.store.get_occupation(device_id)
                if occ is not None and (occ.campaign_id, occ.batch_id) \
                        == (campaign_id, batch_id):
                    continue
                self.store.put_occupation(DeviceOccupation(
                    device_id=device_id, campaign_id=campaign_id,
                    batch_id=batch_id, acquired_at=self.clock()))
                rebuilt += 1
                logger.info(
                    "occupation rebuilt on restore device=%s "
                    "campaign=%s batch=%s", device_id, campaign_id, batch_id)
            if rebuilt or released:
                self.store.save()
            logger.info("occupation restore done rebuilt=%d released=%d",
                        rebuilt, released)
            return {"rebuilt": rebuilt, "released": released}

    def device_occupation(self, device_id: str) -> dict:
        """查询设备当前被哪个活动占着；未被占用时 occupied=False。"""
        with self.store.lock():
            if self.store.get_device(device_id) is None:
                raise NotFoundError("device_not_registered",
                                    f"设备 {device_id} 未注册")
            occupation = self.store.get_occupation(device_id)
            if occupation is None:
                return {"device_id": device_id, "occupied": False}
            return {"device_id": device_id, "occupied": True,
                    "occupation": occupation.to_dict()}

    def device_upgrade_history(self, device_id: str) -> dict:
        """设备历次升级往来，顺序与实际发生顺序一致（重启后仍在）。"""
        with self.store.lock():
            if self.store.get_device(device_id) is None:
                raise NotFoundError("device_not_registered",
                                    f"设备 {device_id} 未注册")
            events = [e.to_dict()
                      for e in self.store.list_upgrade_events(device_id)]
            return {"device_id": device_id, "events": events}

    # ---- 活动生命周期：暂停 / 继续 / 取消 --------------------------------
    def pause_campaign(self, campaign_id: str) -> dict:
        """暂停活动：之后不能开始新批次；重复暂停幂等。"""
        with self.store.lock():
            campaign = self._require_campaign(campaign_id)
            if campaign.state == CampaignState.CANCELLED:
                raise ConflictError(
                    "campaign_cancelled",
                    f"活动 {campaign_id} 已取消，不能暂停")
            campaign.state = CampaignState.PAUSED
            self.store.put_campaign(campaign)
            self.store.save()
            logger.info("campaign paused id=%s", campaign_id)
            return self.campaign_progress(campaign_id)

    def resume_campaign(self, campaign_id: str) -> dict:
        """继续活动：从原断点接着推进；对活动中的活动调用幂等。"""
        with self.store.lock():
            campaign = self._require_campaign(campaign_id)
            if campaign.state == CampaignState.CANCELLED:
                raise ConflictError(
                    "campaign_cancelled",
                    f"活动 {campaign_id} 已取消，不能继续")
            campaign.state = CampaignState.ACTIVE
            self.store.put_campaign(campaign)
            self.store.save()
            logger.info("campaign resumed id=%s", campaign_id)
            return self.campaign_progress(campaign_id)

    def cancel_campaign(self, campaign_id: str,
                        message: str = "活动取消") -> dict:
        """取消活动：只处理仍在进行中的部分并释放设备，已终态部分不动。

        - 进行中批次：整批回滚到各设备升级前版本（只发生一次）；
        - 待开始批次：标记为 cancelled，不占用设备；
        - 已成功/已回滚批次：原样保留。
        重复取消幂等。
        """
        with self.store.lock():
            campaign = self._require_campaign(campaign_id)
            if campaign.state == CampaignState.CANCELLED:
                return self.campaign_progress(campaign_id)

            for batch in campaign.batches:
                if batch.state == BatchState.IN_PROGRESS:
                    # 复用既有整批回滚路径，保证版本恢复只发生一次
                    for up in batch.upgrades.values():
                        up.healthy = False
                        if not up.message:
                            up.message = message
                    self._finalize(campaign, batch)
                elif batch.state == BatchState.PENDING:
                    batch.state = BatchState.CANCELLED
                    # 待开始批次从未占用设备、版本从未变化；仅记录取消往来，
                    # from/to 均为设备当前版本（没有发生实际升级）。
                    for device_id in batch.device_ids:
                        device = self.store.get_device(device_id)
                        current = device.firmware_version
                        self._append_event(
                            device_id, campaign_id, batch.batch_id,
                            UpgradeEventType.CANCELLED,
                            current, current, message)
                    logger.info(
                        "batch cancelled with campaign campaign=%s batch=%s",
                        campaign_id, batch.batch_id)
            campaign.state = CampaignState.CANCELLED
            self.store.put_campaign(campaign)
            self.store.save()
            logger.info("campaign cancelled id=%s", campaign_id)
            return self.campaign_progress(campaign_id)

    def campaign_progress(self, campaign_id: str) -> dict:
        with self.store.lock():
            campaign = self.store.get_campaign(campaign_id)
            if campaign is None:
                raise NotFoundError("campaign_not_found",
                                    f"升级活动 {campaign_id} 不存在")
            batches = [b.to_dict() for b in campaign.batches]
            counts: dict[str, int] = {}
            for b in campaign.batches:
                counts[b.state.value] = counts.get(b.state.value, 0) + 1
            terminal = (BatchState.SUCCEEDED, BatchState.ROLLED_BACK,
                        BatchState.CANCELLED)
            return {
                "campaign_id": campaign_id,
                "firmware_version": campaign.firmware_version,
                "state": campaign.state.value,
                "batch_count": len(batches),
                "state_counts": counts,
                "finished": all(b.state in terminal for b in campaign.batches),
                "batches": batches,
            }
