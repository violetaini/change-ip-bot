from config import config
from utils.logger import logger

from huaweicloudsdkcore.auth.credentials import BasicCredentials
from huaweicloudsdkdns.v2 import DnsClient
from huaweicloudsdkdns.v2.region.dns_region import DnsRegion
from huaweicloudsdkdns.v2.model.list_public_zones_request import ListPublicZonesRequest
from huaweicloudsdkdns.v2.model.list_record_sets_by_zone_request import ListRecordSetsByZoneRequest
from huaweicloudsdkdns.v2.model.update_record_set_request import UpdateRecordSetRequest
from huaweicloudsdkdns.v2.model.update_record_set_req import UpdateRecordSetReq


class HuaweiDNSClient:
    def __init__(self):
        self.ak = str(config.get("huawei_ak", "")).strip()
        self.sk = str(config.get("huawei_sk", "")).strip()
        self.zone_name = str(config.get("dns_zone_name") or config.get("huawei_dns_zone_name", "")).strip()
        self.record_name = str(config.get("dns_record_name") or config.get("huawei_dns_record_name", "")).strip()
        self.record_type = str(config.get("dns_record_type") or config.get("huawei_dns_record_type", "A")).strip().upper()
        self.ttl = int(config.get("dns_ttl") or config.get("huawei_dns_ttl", 60))

    def validate_config(self):
        missing = []
        for key, value in {
            "huawei_ak": self.ak,
            "huawei_sk": self.sk,
            "huawei_dns_zone_name": self.zone_name,
            "huawei_dns_record_name": self.record_name,
        }.items():
            if not value:
                missing.append(key)

        if missing:
            raise RuntimeError(f"华为云DNS配置不完整，缺少: {', '.join(missing)}")

    def _client(self) -> DnsClient:
        self.validate_config()
        credentials = BasicCredentials(self.ak, self.sk)
        return (
            DnsClient.new_builder()
            .with_credentials(credentials)
            .with_region(DnsRegion.value_of("cn-north-4"))
            .build()
        )

    def get_zone_id(self, client: DnsClient) -> str:
        req = ListPublicZonesRequest(name=self.zone_name)
        resp = client.list_public_zones(req)
        zones = getattr(resp, "zones", None) or []
        if not zones:
            raise RuntimeError(f"未找到 Zone: {self.zone_name}")
        return zones[0].id

    def get_recordset(self, client: DnsClient, zone_id: str):
        req = ListRecordSetsByZoneRequest(
            zone_id=zone_id,
            name=self.record_name,
            type=self.record_type,
        )
        resp = client.list_record_sets_by_zone(req)
        recordsets = getattr(resp, "recordsets", None) or []
        if not recordsets:
            raise RuntimeError(
                f"未找到记录集: name={self.record_name}, type={self.record_type}"
            )
        return recordsets[0]

    def update_record(self, new_ip: str) -> str:
        client = self._client()
        zone_id = self.get_zone_id(client)
        recordset = self.get_recordset(client, zone_id)

        req = UpdateRecordSetRequest(
            zone_id=zone_id,
            recordset_id=recordset.id,
            body=UpdateRecordSetReq(
                name=self.record_name,
                type=self.record_type,
                ttl=self.ttl,
                records=[new_ip],
            ),
        )
        client.update_record_set(req)
        logger.info(f"华为云DNS更新成功: {self.record_name} -> {new_ip}")
        return f"{self.record_name} -> {new_ip}"


def update_huawei_dns_if_enabled(new_ip: str) -> str:
    if not config.get("huawei_dns_enabled"):
        return "未启用华为云DNS更新"

    client = HuaweiDNSClient()
    return client.update_record(new_ip)
