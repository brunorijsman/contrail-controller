/*
 * Copyright (c) 2013 Juniper Networks, Inc. All rights reserved.
 */

#include "vnsw/agent/cfg/interface_cfg.h"
#include "sandesh/sandesh_types.h"
#include "sandesh/sandesh.h"
#include <boost/uuid/string_generator.hpp>
#include <boost/uuid/uuid_io.hpp>
#include "base/logging.h"
#include "cfg/cfg_types.h"
#include "cfg/init_config.h"

using boost::uuids::uuid;

// CfgIntData methods
void CfgIntData::Init (const uuid& vm_id, const uuid& vn_id, 
                       const std::string& tname, const IpAddress& ip,
                       const std::string& mac,
                       const std::string& vm_name) {
    vm_id_ = vm_id;
    vn_id_ = vn_id;
    tap_name_ = tname;
    ip_addr_ = ip;
    mac_addr_ = mac;
    vm_name_ = vm_name;
}

// CfgIntEntry methods
void CfgIntEntry::Init(const CfgIntData& int_data) {
    vm_id_ = int_data.vm_id_;
    vn_id_ = int_data.vn_id_;
    tap_name_ = int_data.tap_name_;
    ip_addr_ = int_data.ip_addr_;
    mac_addr_ = int_data.mac_addr_;
    vm_name_ = int_data.vm_name_;
}

bool CfgIntEntry::IsLess(const DBEntry &rhs) const {
    const CfgIntEntry &a = static_cast<const CfgIntEntry &>(rhs);
    return port_id_ < a.port_id_;
}

DBEntryBase::KeyPtr CfgIntEntry::GetDBRequestKey() const {
    CfgIntKey *key = new CfgIntKey(port_id_);
    return DBEntryBase::KeyPtr(key);
}

void CfgIntEntry::SetKey(const DBRequestKey *key) { 
    const CfgIntKey *k = static_cast<const CfgIntKey *>(key);
    port_id_ = k->id_;
}

std::string CfgIntEntry::ToString() const {
    return "Interface Configuration";
}

// CfgIntTable methods
std::auto_ptr<DBEntry> CfgIntTable::AllocEntry(const DBRequestKey *key) const {
    const CfgIntKey *k = static_cast<const CfgIntKey *>(key);
    CfgIntEntry *cfg_intf = new CfgIntEntry(k->id_);
    return std::auto_ptr<DBEntry>(static_cast<DBEntry *>(cfg_intf));
}

DBEntry *CfgIntTable::Add(const DBRequest *req) {
    CfgIntKey *key = static_cast<CfgIntKey *>(req->key.get());
    CfgIntData *data = static_cast<CfgIntData *>(req->data.get());
    CfgIntEntry *cfg_int = new CfgIntEntry(key->id_);
    cfg_int->Init(*data);    
    CfgVnPortKey vn_port_key(cfg_int->GetVnUuid(), cfg_int->GetUuid());
    uuid_tree_[vn_port_key] = cfg_int;

    std::ostringstream vn;
    vn << cfg_int->GetVnUuid();
    std::ostringstream vm;
    vm << cfg_int->GetVmUuid();

    CFG_TRACE(IntfTrace, cfg_int->GetIfname(), 
              cfg_int->GetVmName(), vm.str(), vn.str(), 
              cfg_int->GetIpAddr().to_string(), "ADD");
    return cfg_int;
}

void CfgIntTable::Delete(DBEntry *entry, const DBRequest *req) {
    CfgIntEntry *cfg = static_cast<CfgIntEntry *>(entry);

    std::ostringstream vn;
    vn << cfg->GetVnUuid();
    std::ostringstream vm;
    vm << cfg->GetVmUuid();

    CFG_TRACE(IntfTrace, cfg->GetIfname(), 
              cfg->GetVmName(), vm.str(), vn.str(),
              cfg->GetIpAddr().to_string(), "DELETE");

    CfgVnPortKey vn_port_key(cfg->GetVnUuid(), cfg->GetUuid());
    CfgVnPortTree::iterator it = uuid_tree_.find(vn_port_key);

    assert(it != uuid_tree_.end());
    uuid_tree_.erase(it);
    return;
}

DBTableBase *CfgIntTable::CreateTable(DB *db, const std::string &name) {
    CfgIntTable *table = new CfgIntTable(db, name);
    table->Init();
    return table;
}