import test from 'node:test';
import assert from 'node:assert/strict';
import {selectEthernet} from '../lib/network.mjs';

const ethernet = (name, address, overrides = {}) => ({name, description: 'Ethernet Controller', physical: true, virtual: false, up: true, type: 6, addresses: [address], metric: 25, ...overrides});
test('Ethernet selection rejects virtual, disconnected, wireless, loopback and unassigned IPv4 adapters', () => {
  const candidates = [
    ethernet('VMware Network Adapter VMnet8', '192.168.146.1'),
    ethernet('vEthernet', '172.20.1.1', {virtual: true}),
    ethernet('VPN', '10.0.0.1'),
    ethernet('以太网 2', '192.168.2.1', {physical: false}),
    ethernet('WLAN', '192.168.3.1', {type: 71}),
    ethernet('以太网 3', '192.168.4.1', {up: false}),
    ethernet('以太网 4', '169.254.0.2'),
    ethernet('以太网 5', '127.0.0.1'),
    ethernet('以太网 6', '999.1.1.1'),
    ethernet('以太网', '192.168.10.106'),
  ];
  assert.deepEqual(selectEthernet(candidates), {adapter: '以太网', address: '192.168.10.106'});
  assert.equal(selectEthernet(candidates.slice(0, -1)), null);
});
test('multiple physical Ethernet adapters prefer the lowest default route metric deterministically', () => {
  assert.deepEqual(selectEthernet([ethernet('USB Ethernet', '192.168.2.5', {metric: 2147483647}), ethernet('以太网', '192.168.10.106', {metric: 35})]), {adapter: '以太网', address: '192.168.10.106'});
});
