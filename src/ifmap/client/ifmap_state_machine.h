/*
 * Copyright (c) 2013 Juniper Networks, Inc. All rights reserved.
 */

#ifndef __IFMAP_STATE_MACHINE_H__
#define __IFMAP_STATE_MACHINE_H__

#include <boost/asio/deadline_timer.hpp>
#include <boost/statechart/state_machine.hpp>

#include "base/queue_task.h"

namespace sc = boost::statechart;

class IFMapChannel;
class IFMapManager;

namespace ifsm {
struct EvStart;
struct Idle;
struct EvReadSuccess;
}

class IFMapStateMachine :
        public sc::state_machine<IFMapStateMachine, ifsm::Idle> {
public:
    static const int kConnectInterval;
    static const int kResponseWaitInterval;

    enum State {
        IDLE                   = 0,
        SERVERRESOLVE          = 1,
        SSRCCONNECT            = 2,
        SSRCSSLHANDSHAKE       = 3,
        SENDNEWSESSION         = 4,
        NEWSESSIONRESPONSEWAIT = 5,
        SENDSUBSCRIBE          = 6,
        SUBSCRIBERESPONSEWAIT  = 7,
        ARCCONNECT             = 8,
        ARCSSLHANDSHAKE        = 9,
        SENDPOLL               = 10,
        POLLRESPONSEWAIT       = 11,
        SSRCSTART              = 12,
        CONNECTTIMERWAIT       = 13
        // Add an entry to state_names[] if you add an entry here
    };

    IFMapStateMachine(IFMapManager *manager);

    void Initialize();

    int GetConnectTime(bool is_ssrc) const;

    void StartConnectTimer(int seconds);

    void StopConnectTimer();

    void ConnectTimerExpired(const boost::system::error_code& error);

    void StartResponseTimer();

    void StopResponseTimer();

    void ResponseTimerExpired(const boost::system::error_code& error);

    void Start();

    void ProcConnectionCleaned();

    void ProcResolveResponse(const boost::system::error_code& error);

    void ProcConnectResponse(const boost::system::error_code& error);

    void ProcHandshakeResponse(const boost::system::error_code& error);

    void ProcNewSessionWrite(const boost::system::error_code& error,
                             size_t bytes_transferred);
    void ProcNewSessionResponse(const boost::system::error_code& error,
                                size_t bytes_transferred);
    void ProcSubscribeWrite(const boost::system::error_code& error,
                            size_t bytes_transferred);
    void ProcSubscribeResponse(const boost::system::error_code& error,
                               size_t bytes_transferred);
    void ProcSubscribeResponseStr(bool error, std::string resp_str);

    void ProcPollWrite(const boost::system::error_code& error,
                       size_t bytes_transferred);
    void ProcPollResponseRead(const boost::system::error_code& error,
                                size_t bytes_transferred);
    void ProcPollRespBodyRead(const boost::system::error_code& error,
                              size_t bytes_transferred);
    void ProcResponse(const boost::system::error_code& error,
                      size_t bytes_transferred);

    void set_channel(IFMapChannel *channel) {
        channel_ = channel;
    }

    void ssrc_connect_attempts_inc() { ssrc_connect_attempts_++; }
    void ssrc_connect_attempts_clear() { ssrc_connect_attempts_ = 0; }

    void arc_connect_attempts_inc() { arc_connect_attempts_++; }
    void arc_connect_attempts_clear() { arc_connect_attempts_ = 0; }

    int connection_attempts_get() { return connection_attempts_; }

    void OnStart(const ifsm::EvStart &event);
    bool DequeueEvent(boost::intrusive_ptr<const sc::event_base> &event);

    IFMapChannel *channel() { return channel_; }

    void set_state(State state);
    State state() { return state_; }
    State last_state() { return last_state_; }
    const std::string &StateName() const;
    const std::string &LastStateName() const;
    const std::string &StateName(State state) const;
    const std::string last_state_change_at() const;

    void set_last_event(const std::string &event);
    const std::string &last_event() const { return last_event_; }
    const std::string last_event_at() const;

private:
    void EnqueueEvent(const sc::event_base &ev);
    void connection_attempts_inc() { connection_attempts_++; }
    void connection_attempts_clear() { connection_attempts_ = 0; }

    IFMapManager *manager_;
    boost::asio::deadline_timer connect_timer_;
    int ssrc_connect_attempts_;
    int arc_connect_attempts_;
    // used to limit the wait time for a response
    boost::asio::deadline_timer response_timer_;
    // how many times we timed out waiting for a response
    int connection_attempts_;
    IFMapChannel *channel_;
    WorkQueue<boost::intrusive_ptr<const sc::event_base> > work_queue_;

    State state_;
    State last_state_;
    uint64_t last_state_change_at_;
    std::string last_event_;
    uint64_t last_event_at_;
};

#endif /* __IFMAP_STATE_MACHINE_H__ */
