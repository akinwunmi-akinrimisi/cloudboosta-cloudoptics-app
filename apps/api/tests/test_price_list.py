"""The AWS Price List reader behind migrations/data/price_list.json (WBS IN-9)."""

from __future__ import annotations

import json

import pytest

from cloudoptics.analysis import price_list

HEADER = (
    '"TermType","PricePerUnit","StartingRange","Product Family","Location Type","usageType",'
    '"Instance Type","Tenancy","Operating System","Pre Installed S/W","CapacityStatus",'
    '"License Model","Database Engine","Deployment Option"'
)


def csv_text(*lines: str) -> list[str]:
    meta = ['"FormatVersion","v1.0"', '"Disclaimer","x"', '"Publication Date","x"',
            '"Version","x"', '"OfferCode","x"']  # fmt: skip
    return [f"{line}\n" for line in [*meta, HEADER, *lines]]


def test_items_from_offer_rows() -> None:
    linux = '"Shared","Linux","NA","Used","No License required",,'
    text = csv_text(
        f'"OnDemand","0.116","0","Storage","AWS Region","EUW2-EBS:VolumeUsage.gp2",{"," * 8}',
        f'"OnDemand","47.5136","0","Provisioned Throughput","AWS Region",'
        f'"EUW2-EBS:VolumeP-Throughput.gp3",{"," * 8}',
        f'"OnDemand","0.005","0","IP Address","AWS Region","EUW2-PublicIPv4:IdleAddress",{"," * 8}',
        f'"OnDemand","0.02646","0","Load Balancer-Application","AWS Region",'
        f'"EUW2-LoadBalancerUsage",{"," * 8}',
        # Trust stores and Outposts share the usage type's tail but are other products.
        f'"OnDemand","0.0059","0","Load Balancer-Application","AWS Region",'
        f'"EUW2-TS-LoadBalancerUsage",{"," * 8}',
        f'"OnDemand","0.0294","0","Load Balancer","AWS Region","EUW2-LoadBalancerUsage",{"," * 8}',
        f'"OnDemand","0.111","0","Compute Instance","AWS Region","EUW2-BoxUsage:m5.large",'
        f'"m5.large",{linux}',
        f'"OnDemand","0.444","0","Compute Instance","AWS Region","EUW2-BoxUsage:m5.2xlarge",'
        f'"m5.2xlarge",{linux}',
        # No large size: the xlarge price halved.
        f'"OnDemand","0.4","0","Compute Instance","AWS Region","EUW2-BoxUsage:x9.xlarge",'
        f'"x9.xlarge",{linux}',
        # Windows, reserved terms and Local Zones are not list prices we use.
        '"OnDemand","0.2","0","Compute Instance","AWS Region","EUW2-BoxUsage:m6i.large",'
        '"m6i.large","Shared","Windows","NA","Used","No License required",,',
        f'"Reserved","0.05","0","Compute Instance","AWS Region","EUW2-BoxUsage:c5.large",'
        f'"c5.large",{linux}',
        f'"OnDemand","0.5","0","Storage","AWS Local Zone","EUW2-EBS:VolumeUsage.gp3",{"," * 8}',
        '"OnDemand","0.206","0","Database Instance","AWS Region",'
        '"EUW2-InstanceUsage:db.m5.large","db.m5.large",,,,,,"PostgreSQL","Single-AZ"',
        '"OnDemand","0.412","0","Database Instance","AWS Region",'
        '"EUW2-Multi-AZUsage:db.m5.large","db.m5.large",,,,,,"PostgreSQL","Multi-AZ"',
        '"OnDemand","0.198","0","Database Instance","AWS Region",'
        '"EUW2-InstanceUsage:db.m5.large","db.m5.large",,,,,,"Oracle","Single-AZ"',
    )
    assert price_list.items(price_list.rows(text)) == {
        "alb_hour": 0.02646,
        "clb_hour": 0.0294,
        "ebs_gp2_gb_month": 0.116,
        "ebs_gp3_throughput_month": pytest.approx(0.0464),
        "ec2_m5_large_hour": 0.111,
        "ec2_x9_large_hour": 0.2,
        "public_ipv4_hour": 0.005,
        "rds_postgres_m5_large_hour": 0.206,
    }


def test_regions_are_aws_regions_only() -> None:
    assert price_list.REGION.match("eu-west-2")
    assert price_list.REGION.match("us-gov-west-1")
    assert not price_list.REGION.match("us-east-1-bos-1")
    assert not price_list.REGION.match("eu-west-2-wl1-lon1")


def test_generated_prices_cover_every_region() -> None:
    prices = json.loads(price_list.OUT.read_text())
    core = {item for item, _ in price_list.USAGE_ITEMS.values()} | set(price_list.LB_ITEMS.values())
    assert len(prices) >= 30
    for region, items in prices.items():
        assert price_list.REGION.match(region)
        assert core <= set(items), region
    assert prices["eu-west-2"]["ebs_gp2_gb_month"] == 0.116
    assert prices["us-east-1"]["ec2_m5_large_hour"] == 0.096
