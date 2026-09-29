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
    DeviceOccupancy,
    DeviceUpgrade,
    DeviceUpgradeState,
    FirmwareImage,
    UpgradeEvent,
    UpgradeEventKind,
)
from app.store import Store

logger = logging.getLogger("iot.upgrade")


class UpgradeService:
    def __init__(self, store: Store, clock: Callable[[], float] | None = None):
        self.store = store
        self.clock = clock or time.time
        # 以快照中「进行中的批次」为权威来源重建占用关系，保证重启后
        # 占用、活动、设备版本与重启前一致，杜绝重启后设备被第二活动收走。
        self._rebuild_occupancy()

    def _rebuild_occupancy(self) -> None:
        occs: dict[str, DeviceOccupancy] = {}
        for campaign in self.store.list_campaigns():
            if campaign.state == CampaignState.CANCELLED:
                continue
            for batch in campaign.batches:
                if batch.state != BatchState.IN_PROGRESS:
                    continue
                for device_id in batch.device_ids:
                    prev = self.store.get_occupancy(device_id)
                    if (prev is not None
                            and prev.campaign_id == campaign.campaign_id
                            and prev.batch_id == batch.batch_id):
                        occs[device_id] = prev
                    else:
                        occs[device_id] = DeviceOccupancy(
                            device_id=device_id,
                            campaign_id=campaign.campaign_id,
                            batch_id=batch.batch_id)
        self.store.replace_all_occupancy(occs)
        if occs:
            logger.info("occupancy rebuilt on startup devices=%d", len(occs))

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
            if batch.state == BatchState.CANCELLED:
                raise ConflictError("batch_cancelled",
                                    f"批次 {batch_id} 已随活动取消，终止态")
            if batch.state == BatchState.IN_PROGRESS:
                raise ConflictError("batch_in_progress",
                                    f"批次 {batch_id} 正在升级中")
            # 严格按批次顺序推进
            for prev in campaign.batches:
                if prev.order < batch.order and prev.state != BatchState.SUCCEEDED:
                    raise ConflictError(
                        "previous_batch_not_succeeded",
                        f"前序批次 {prev.batch_id} 状态为 {prev.state.value}")

            # 暂停中的活动不得再推进新批次；已取消活动彻底拒绝
            if campaign.state == CampaignState.PAUSED:
                raise ConflictError(
                    "campaign_paused",
                    f"升级活动 {campaign_id} 已暂停，无法推进批次")
            if campaign.state == CampaignState.CANCELLED:
                raise ConflictError(
                    "campaign_cancelled",
                    f"升级活动 {campaign_id} 已取消，无法推进批次")

            # 设备占用门禁：同一设备同一时刻只能被一个进行中的活动占用。
            # 并发争用时恰好一个成功，其余立即以独立原因 device_occupied
            # 拒绝（不改动该设备版本，也不影响已推进中的另一边）。
            for device_id in batch.device_ids:
                occ = self.store.get_occupancy(device_id)
                if occ is not None:
                    raise ConflictError(
                        "device_occupied",
                        f"设备 {device_id} 正被升级活动 {occ.campaign_id} "
                        f"批次 {occ.batch_id} 占用")

            target = campaign.firmware_version
            now = self.clock()
            # 进入 IN_PROGRESS 并先落盘（中途失败重启也能看到一致中间态）
            for device_id in batch.device_ids:
                device = self.store.get_device(device_id)
                device.upgrade_state = DeviceUpgradeState.UPGRADING
                device.target_version = target
                device.campaign_id = campaign_id
                self.store.put_device(device)
                batch.upgrades[device_id] = DeviceUpgrade(
                    device_id=device_id,
                    from_version=device.firmware_version,
                    to_version=target,
                    state=DeviceUpgradeState.UPGRADING,
                )
                # 申请占用（上面已统一检查，此处幂等写入归属本批次）
                self.store.put_occupancy(DeviceOccupancy(
                    device_id=device_id, campaign_id=campaign_id,
                    batch_id=batch_id, occupied_at=now))
                # 记录升级往来的起点
                self.store.append_upgrade_event(UpgradeEvent(
                    seq=0, device_id=device_id, campaign_id=campaign_id,
                    batch_id=batch_id, kind=UpgradeEventKind.STARTED,
                    from_version=device.firmware_version, to_version=target,
                    ts=now))
            batch.state = BatchState.IN_PROGRESS
            campaign.state = CampaignState.ACTIVE
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

    def _finalize(self, campaign: Campaign, batch: Batch,
                  cancel_message: str = "") -> dict:
        now = self.clock()
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
                        up.message = cancel_message or "健康检查失败，随批次回滚"
                else:
                    up.state = DeviceUpgradeState.ROLLED_BACK
                    device.upgrade_state = DeviceUpgradeState.ROLLED_BACK
                    up.message = (
                        up.message
                        or cancel_message
                        or "同批存在失败设备，整批回滚")
                self.store.put_device(device)
                # 终态释放占用，并记录回滚往来（版本恢复 from_version）
                self.store.remove_occupancy(up.device_id)
                self.store.append_upgrade_event(UpgradeEvent(
                    seq=0, device_id=up.device_id,
                    campaign_id=campaign.campaign_id, batch_id=batch.batch_id,
                    kind=UpgradeEventKind.ROLLED_BACK,
                    from_version=up.to_version, to_version=up.from_version,
                    ts=now, message=up.message))
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
                # 终态释放占用，并记录成功往来（版本推进到目标版本）
                self.store.remove_occupancy(up.device_id)
                self.store.append_upgrade_event(UpgradeEvent(
                    seq=0, device_id=up.device_id,
                    campaign_id=campaign.campaign_id, batch_id=batch.batch_id,
                    kind=UpgradeEventKind.SUCCEEDED,
                    from_version=up.from_version, to_version=up.to_version,
                    ts=now, message=up.message))
            batch.state = BatchState.SUCCEEDED
            logger.info("batch succeeded campaign=%s batch=%s",
                        campaign.campaign_id, batch.batch_id)
        self.store.put_campaign(campaign)
        self.store.save()
        return batch.to_dict()

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
            return {
                "campaign_id": campaign_id,
                "firmware_version": campaign.firmware_version,
                "state": campaign.state.value,
                "batch_count": len(batches),
                "state_counts": counts,
                "finished": all(b.state in (BatchState.SUCCEEDED,
                                            BatchState.ROLLED_BACK,
                                            BatchState.CANCELLED)
                                for b in campaign.batches),
                "batches": batches,
            }

    # ---- 活动生命周期：暂停 / 继续 / 取消 --------------------------------
    def pause_campaign(self, campaign_id: str) -> dict:
        """暂停活动：不得再推进新批次；进行中的批次可正常上报/终结。

        重复暂停幂等：已暂停直接返回，不改动任何批次与设备版本。
        """
        with self.store.lock():
            campaign = self._require_campaign(campaign_id)
            if campaign.state == CampaignState.CANCELLED:
                raise ConflictError(
                    "campaign_cancelled",
                    f"升级活动 {campaign_id} 已取消，不能暂停")
            if campaign.state == CampaignState.PAUSED:
                logger.info("campaign pause idempotent id=%s", campaign_id)
                return self.campaign_progress(campaign_id)
            campaign.state = CampaignState.PAUSED
            self.store.put_campaign(campaign)
            self.store.save()
            logger.info("campaign paused id=%s", campaign_id)
            return self.campaign_progress(campaign_id)

    def resume_campaign(self, campaign_id: str) -> dict:
        """继续活动：从原断点接着按批次顺序推进（不触碰任何设备版本）。

        重复继续幂等：活动本就在运行中时直接返回当前进度。
        """
        with self.store.lock():
            campaign = self._require_campaign(campaign_id)
            if campaign.state == CampaignState.CANCELLED:
                raise ConflictError(
                    "campaign_cancelled",
                    f"升级活动 {campaign_id} 已取消，不能继续")
            if campaign.state == CampaignState.ACTIVE:
                logger.info("campaign resume idempotent id=%s", campaign_id)
                return self.campaign_progress(campaign_id)
            campaign.state = CampaignState.ACTIVE
            self.store.put_campaign(campaign)
            self.store.save()
            logger.info("campaign resumed id=%s", campaign_id)
            return self.campaign_progress(campaign_id)

    def cancel_campaign(self, campaign_id: str) -> dict:
        """取消活动：只处理仍在进行中的部分并释放设备。

        - IN_PROGRESS 批次：走与人工回滚相同的整批回滚路径，设备版本恢复
          升级前版本，占用释放，并记一条回滚往来；
        - PENDING 批次：置为 CANCELLED（取消态，不动设备、不记往来）；
        - SUCCEEDED / ROLLED_BACK 批次：终态不动，已成功部分不受影响；
        - 重复取消幂等：已取消直接返回，绝不重复回滚或改动版本。
        """
        with self.store.lock():
            campaign = self._require_campaign(campaign_id)
            if campaign.state == CampaignState.CANCELLED:
                logger.info("campaign cancel idempotent id=%s", campaign_id)
                return self.campaign_progress(campaign_id)

            cancel_message = f"活动 {campaign_id} 已取消，随活动回滚"
            for batch in campaign.batches:
                if batch.state == BatchState.IN_PROGRESS:
                    for up in batch.upgrades.values():
                        up.healthy = False
                        if not up.message:
                            up.message = cancel_message
                    self._finalize(campaign, batch,
                                   cancel_message=cancel_message)
                elif batch.state == BatchState.PENDING:
                    batch.state = BatchState.CANCELLED
            campaign.state = CampaignState.CANCELLED
            self.store.put_campaign(campaign)
            self.store.save()
            logger.info("campaign cancelled id=%s", campaign_id)
            return self.campaign_progress(campaign_id)

    def _require_campaign(self, campaign_id: str) -> Campaign:
        campaign = self.store.get_campaign(campaign_id)
        if campaign is None:
            raise NotFoundError("campaign_not_found",
                                f"升级活动 {campaign_id} 不存在")
        return campaign

    # ---- 占用与升级往来查询 ----------------------------------------------
    def device_occupancy(self, device_id: str) -> dict:
        """查询设备当前被哪个活动/批次占着；未被占用时 occupied=False。"""
        with self.store.lock():
            device = self.store.get_device(device_id)
            if device is None:
                raise NotFoundError("device_not_registered",
                                    f"设备 {device_id} 未注册")
            occ = self.store.get_occupancy(device_id)
            if occ is None:
                return {"device_id": device_id, "occupied": False}
            return {"device_id": device_id, "occupied": True,
                    "campaign_id": occ.campaign_id,
                    "batch_id": occ.batch_id,
                    "occupied_at": occ.occupied_at}

    def device_upgrade_history(self, device_id: str) -> dict:
        """查询设备历次版本变化的来龙去脉，顺序与实际发生顺序一致。"""
        with self.store.lock():
            device = self.store.get_device(device_id)
            if device is None:
                raise NotFoundError("device_not_registered",
                                    f"设备 {device_id} 未注册")
            events = [e.to_dict()
                      for e in self.store.list_upgrade_events(device_id)]
            return {"device_id": device_id,
                    "current_version": device.firmware_version,
                    "events": events}
