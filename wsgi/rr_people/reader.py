# coding=utf-8
import json
import logging
import time
from datetime import datetime
from multiprocessing import Process
from multiprocessing.process import current_process

import redis

from wsgi.db import DBHandler
from wsgi.properties import min_copy_count, \
    shift_copy_comments_part, min_donor_comment_ups, max_donor_comment_ups, \
    comments_mongo_uri, comments_db_name, DEFAULT_LIMIT, cfs_redis_address, cfs_redis_port, cfs_redis_password
from wsgi.rr_people import RedditHandler, cmp_by_created_utc, post_to_dict, S_WORK, S_END
from wsgi.rr_people import re_url, normalize, re_crying_chars
from wsgi.rr_people.queue import CommentQueue
from wsgi.rr_people.states.persist import ProcessStatesPersist

log = logging.getLogger("reader")


def _so_long(created, min_time):
    return (datetime.utcnow() - datetime.fromtimestamp(created)).total_seconds() > min_time


def is_good_text(text):
    return len(re_url.findall(text)) == 0 and \
           len(text) > 15 and \
           len(text) < 120 and \
           "Edit" not in text


PERSIST_STATE = lambda x: "load_state_%s" % x

START_TIME = "t_start"
END_TIME = "t_end"
LOADED_COUNT = "loaded_count"

PREV_START_TIME = "p_t_start"
PREV_END_TIME = "p_t_end"
PREV_LOADED_COUNT = "p_loaded_count"

IS_ENDED = "ended"
IS_STARTED = "started"
PROCESSED_COUNT = "processed_count"
CURRENT = "current"


class CommentFounderStateStorage(object):
    def __init__(self, name="?", clear=False, max_connections=2):
        self.redis = redis.StrictRedis(host=cfs_redis_address,
                                       port=cfs_redis_port,
                                       password=cfs_redis_password,
                                       db=0,
                                       max_connections=max_connections
                                       )
        if clear:
            self.redis.flushdb()

        log.info("Comment founder state storage for %s inited!" % name)

    def persist_load_state(self, sub, start, stop, count):
        p = self.redis.pipeline()

        key = PERSIST_STATE(sub)
        persisted_state = self.redis.hgetall(key)
        if persisted_state:
            p.hset(key, PREV_START_TIME, persisted_state.get(START_TIME))
            p.hset(key, PREV_END_TIME, persisted_state.get(END_TIME))
            p.hset(key, PREV_LOADED_COUNT, persisted_state.get(LOADED_COUNT))
            p.hset(key, PROCESSED_COUNT, 0)
            p.hset(key, CURRENT, json.dumps({}))

        p.hset(key, START_TIME, start)
        p.hset(key, END_TIME, stop)
        p.hset(key, LOADED_COUNT, count)
        p.execute()

    def set_ended(self, sub):
        p = self.redis.pipeline()
        p.hset(PERSIST_STATE(sub), IS_ENDED, True)
        p.hset(PERSIST_STATE(sub), IS_STARTED, False)
        p.execute()

    def set_started(self, sub):
        p = self.redis.pipeline()
        p.hset(PERSIST_STATE(sub), IS_ENDED, False)
        p.hset(PERSIST_STATE(sub), IS_STARTED, True)
        p.execute()

    def is_ended(self, sub):
        return self.redis.hget(PERSIST_STATE(sub), IS_ENDED)

    def is_started(self, sub):
        return self.redis.hget(PERSIST_STATE(sub), IS_STARTED)

    def set_current(self, sub, current):
        p = self.redis.pipeline()
        p.hset(PERSIST_STATE(sub), CURRENT, json.dumps(current))
        p.hincrby(PERSIST_STATE(sub), PROCESSED_COUNT, 1)
        p.execute()

    def get_current(self, sub):
        data = self.redis.hget(PERSIST_STATE(sub), CURRENT)
        if data:
            return json.loads(data)

    def get_proc_count(self, sub):
        return self.redis.hget(PERSIST_STATE(sub), PROCESSED_COUNT)

    def get_state(self, sub):
        return self.redis.hgetall(PERSIST_STATE(sub))

    def reset_state(self, sub):
        self.redis.hdel(PERSIST_STATE(sub), *self.redis.hkeys(PERSIST_STATE(sub)))
        return


class CommentsStorage(DBHandler):
    def __init__(self, name="?"):
        super(CommentsStorage, self).__init__(name=name, uri=comments_mongo_uri, db_name=comments_db_name)
        collections_names = self.db.collection_names(include_system_collections=False)

        self.comments = self.db.get_collection("comments")
        if "comments" not in collections_names:
            self.comments = self.db.create_collection(
                "comments",
                capped=True,
                size=1024 * 1024 * 256,
            )
            self.comments.drop_indexes()

            self.comments.create_index([("fullname", 1)], unique=True)
            self.comments.create_index([("commented", 1)], sparse=True)
            self.comments.create_index([("ready_for_comment", 1)], sparse=True)
            self.comments.create_index([("text_hash", 1)], sparse=True)
            self.comments.create_index([("sub", 1)], sparse=True)
        else:
            self.comments = self.db.get_collection("comments")

    def set_post_commented(self, post_fullname, by, hash):
        found = self.comments.find_one({"fullname": post_fullname, "commented": {"$exists": False}})
        if not found:
            to_add = {"fullname": post_fullname, "commented": True, "time": time.time(), "text_hash": hash, "by": by}
            self.comments.insert_one(to_add)
        else:
            to_set = {"commented": True, "text_hash": hash, "by": by, "time": time.time(),
                      "low_copies": datetime.utcnow()}
            self.comments.update_one({"fullname": post_fullname}, {"$set": to_set})

    def can_comment_post(self, who, post_fullname, hash):
        q = {"by": who, "commented": True, "$or": [{"fullname": post_fullname}, {"text_hash": hash}]}
        found = self.comments.find_one(q)
        return found is None

    def set_post_ready_for_comment(self, post_fullname, sub, comment_text, permalink):
        found = self.comments.find_one({"fullname": post_fullname})
        if found:
            return
        else:
            return self.comments.insert_one(
                {"fullname": post_fullname,
                 "ready_for_comment": True,
                 "sub": sub,
                 "text": comment_text,
                 "post_url": permalink})

    def get_posts_ready_for_comment(self, sub=None):
        q = {"ready_for_comment": True, "commented": {"$exists": False}}
        if sub:
            q['sub'] = sub
        return list(self.comments.find(q))

    def get_post(self, post_fullname):
        found = self.comments.find_one({"fullname": post_fullname})
        return found

    def get_posts_commented(self, by=None, sub=None):
        q = {"commented": True}
        if by:
            q["by"] = by
        if sub:
            q['sub'] = sub
        return list(self.comments.find(q))

    def get_posts(self, posts_fullnames):
        for el in self.comments.find({"fullname": {"$in": posts_fullnames}},
                                     projection={"text": True, "fullname": True, "post_url": True}):
            yield el


cs_aspect = lambda x: "CS_%s" % x
is_cs_aspect = lambda x: x.count("CS_") == 1
cs_sub = lambda x: x.replace("CS_", "") if isinstance(x, (str, unicode)) and is_cs_aspect(x) else x


class CommentSearcher(RedditHandler):
    def __init__(self, user_agent=None):
        """
        :param user_agent: for reddit non auth and non oauth client
        :param lcp: low copies posts if persisted
        :param cp:  commented posts if persisted
        :return:
        """
        super(CommentSearcher, self).__init__(user_agent)
        self.comment_storage = CommentsStorage(name="comment_searcher")
        self.comment_queue = CommentQueue(name="comment_searcher")
        self.state_storage = CommentFounderStateStorage(name="comment_searcher")
        self.state_persist = ProcessStatesPersist(name="comment_searcher")
        self.processes = {}

        self.start_supply_comments()
        log.info("comment searcher inited!")

    def _start(self, aspect):
        _aspect = cs_aspect(aspect)
        _pid = current_process().pid
        started = self.state_persist.start_aspect(_aspect, _pid)
        if started.get("started", False):
            self.state_persist.set_state(_aspect, S_WORK)
            self.state_persist.set_state_data(_aspect, {"state": "started", "by": _pid})
            return True
        return False

    def _stop(self, aspect):
        _aspect = cs_aspect(aspect)
        self.state_persist.stop_aspect(_aspect)
        self.state_persist.set_state(_aspect, S_END)
        self.state_persist.set_state_data(_aspect, {"state": "stopped"})

    def comment_retrieve_iteration(self, sub):
        started = self._start(sub)
        if not started:
            log.info("Can not start comment retrieve iteration in [%s] because already started" % sub)
            return

        log.info("Will start find comments for [%s]" % (sub))
        try:
            for pfn in self.find_comment(sub):
                self.comment_queue.put_comment(sub, pfn)
        except Exception as e:
            log.exception(e)

        self._stop(sub)

    def start_comment_retrieve_iteration(self, sub):
        if sub in self.processes and self.processes[sub].is_alive():
            log.info("process for sub [%s] already work" % sub)
            return

        def f():
            self.comment_retrieve_iteration(sub)

        process = Process(name="csp [%s]" % sub, target=f)
        process.daemon = True
        process.start()
        self.processes[sub] = process

    def start_supply_comments(self):
        start = self._start("supply_comments")
        if not start:
            log.info("Can not supply because already supplied")
            return

        def f():
            log.info("Start supplying comments")
            for message in self.comment_queue.get_who_needs_comments():
                nc_sub = message.get("data")
                log.info("Receive need comments for sub [%s]" % nc_sub)
                self.start_comment_retrieve_iteration(nc_sub)

        process = Process(name="comment supplier", target=f)
        process.start()

    def _get_posts(self, sub):
        state = self.state_storage.get_state(sub)
        limit = DEFAULT_LIMIT
        if state:
            if state.get(IS_ENDED) == "True":
                end = float(state.get(END_TIME))
                start = float(state.get(START_TIME))
                loaded_count = float(state.get(LOADED_COUNT))

                _limit = ((time.time() - end) * loaded_count) / ((end - start) or 1.0)
                if _limit < 1:
                    _limit = 25
            else:
                _limit = int(state.get(LOADED_COUNT, DEFAULT_LIMIT)) - int(state.get(PROCESSED_COUNT, 0))

            limit = _limit if _limit < DEFAULT_LIMIT else DEFAULT_LIMIT

        posts = self.get_hot_and_new(sub, sort=cmp_by_created_utc, limit=limit)
        current = self.state_storage.get_current(sub)
        if current:
            posts = filter(lambda x: x.created_utc > current.get("created_utc"), posts)

        if len(posts):
            self.state_storage.persist_load_state(sub, posts[0].created_utc, posts[-1].created_utc, len(posts))
        return posts

    def _get_acceptor(self, posts):
        posts.sort(cmp_by_created_utc)
        half_avg = float(reduce(lambda x, y: x + y.num_comments, posts, 0)) / (len(posts) * 2)
        for post in posts:
            if not post.archived and post.num_comments < half_avg:
                return post

    def find_comment(self, sub, add_authors=False):
        posts = self._get_posts(sub)
        self.state_persist.set_state_data(cs_aspect(sub), {"state": S_WORK, "retrieved": len(posts)})
        self.state_storage.set_started(sub)
        log.info("Start finding comments to sub %s" % sub)
        for post in posts:
            self.state_storage.set_current(sub, post_to_dict(post))

            try:
                copies = self._get_post_copies(post)
                if len(copies) >= min_copy_count:
                    post = self._get_acceptor(copies)
                    comment = None
                    for copy in copies:
                        if post and copy.subreddit != post.subreddit and copy.fullname != post.fullname:
                            comment = self._retrieve_interested_comment(copy, post)
                            if comment:
                                log.info("Find comment: [%s]\n in post: [%s] (%s) at subreddit: [%s]" % (
                                    comment.body, post, post.fullname, sub))
                                break

                    if comment and self.comment_storage.set_post_ready_for_comment(post.fullname, sub, comment.body,
                                                                                   post.permalink):
                        self.state_persist.set_state_data(cs_aspect(sub), {"state": "found", "for": post.fullname})
                        yield post.fullname

            except Exception as e:
                log.exception(e)

        self.state_storage.set_ended(sub)

    def _get_post_copies(self, post):
        search_request = "url:\'%s\'" % post.url
        copies = list(self.reddit.search(search_request)) + [post]
        return list(copies)

    def _retrieve_interested_comment(self, copy, post):
        # prepare comments from donor to selection
        after = copy.num_comments / shift_copy_comments_part
        if not after:
            return
        if after > 34:
            after = 34
        for i, comment in enumerate(self.comments_sequence(copy.comments)):
            if i < after:
                continue
            if comment.ups >= min_donor_comment_ups and \
                            comment.ups <= max_donor_comment_ups and \
                            post.author != comment.author and \
                    self._check_comment_text(comment.body, post):
                return comment

    def _check_comment_text(self, text, post):
        """
        Checking in db, and by is good and found similar text in post comments.
        Similar it is when tokens (only words) have equal length and full intersection
        :param text:
        :param post:
        :return:
        """
        if is_good_text(text):
            c_tokens = set(normalize(text, lambda x: x))
            if (float(len(c_tokens)) / 100) * 20 >= len(re_crying_chars.findall(text)):
                for p_comment in self.get_all_comments(post):
                    p_text = p_comment.body
                    if is_good_text(p_text):
                        p_tokens = set(normalize(p_text, lambda x: x))
                        if len(c_tokens) == len(p_tokens) and len(p_tokens.intersection(c_tokens)) == len(p_tokens):
                            log.info("found similar text [%s] in post %s" % (c_tokens, post.fullname))
                            return False
                self.clear_cache(post)
                return True
