# -*- coding: utf-8 -*-

# Copyright 2009-2015 Jaap Karssenberg <jaap.karssenberg@gmail.com>


from __future__ import with_statement


### ISSUES ###
#
# Some way for main thread to inspect progress -- for progress dialog
#	report stage (scanning tree, indexing pages)
#	x out of n (done since start of update versus total flagged to do)
#  - use the probably-up-to-date here as well ?
#
# Try to use multiprocessing instead of threading for the indexer ?
#
# Issue with page exists flag: since PAGE_EXISTS_AS_LINK overrules
# PAGE_EXISTS_UNCERTAIN, methods like check_pagelist and update_children
# can miss pages that are flagged as placeholder, while existing as
# well as folder in the store
# Solve be "page_exists_in_store" flag, or separate table of files &
# folders from table of notebook nodes ...
#
##############

# Flow for indexer when checking a page:
#
#  queue
#    |
#    |--> CHECK_TREE
#    |       |    check etag_children
#    |       |     add / remove children
#    |       |      recursive for all children
#    |       V
#    |--> CHECK_PAGE
#    |       | |  check etag_content
#    |       | |   index content
#    |       | |    check etag_children
#    |       | V
#    `--> CHECK_CHILDREN
#            | |  check etag_children
#            | |    add / remove children
#            | |     recursive for new / changed children only
#            V V
#          UPTODATE
#
# The indexer prioritizes CHECK_CHILDREN and CHECK_TREE over CHECK_PAGE.
# As a result we first walk the whole tree structure before starting
# to idex content.
#
# The "on_store_page" and "on_delete_page" calls are typically called
# in another thread. Will aquire lock and interrupt the indexer. This
# way interactive changes from the GUI always are handled immediatly.


import sqlite3
import threading
import logging

logger = logging.getLogger('zim.notebook.index')

from zim.utils.threading import WorkerThread
from zim.fs import File

from zim.newfs import FileNotFoundError

from .base import *
from .pages import *
from .links import *
from .tags import *


DB_VERSION = '0.6'

# Constants for the "needsheck" column in the "pages" table
# Lower numbers take precedence while processing
INDEX_UPTODATE = 0		 # No update needed
INDEX_NEED_UPDATE_CHILDREN = 1  # TODO - base probaby uptodate on this
INDEX_NEED_UPDATE_PAGE = 2  # TODO - base probaby uptodate on this
INDEX_CHECK_TREE = 3     # Check if children need to be updated, followed by a check page, recursive
INDEX_CHECK_CHILDREN = 4 # Check if children need to be updated, do nothing if children etag OK
INDEX_CHECK_PAGE = 5     # Check if page needs to be updated, do nothing if both etag OK


INDEX_INIT_SCRIPT = '''
CREATE TABLE zim_index (
	key TEXT,
	value TEXT,
	CONSTRAINT uc_MetaOnce UNIQUE (key)
);
INSERT INTO zim_index VALUES ('db_version', %r);
INSERT INTO zim_index VALUES ('probably_uptodate', 0);
''' % DB_VERSION


class Index(object):
	'''The Index keeps a cache of all pages in a notebook store, all
	links between pages and all tags. This data is used to speed up
	many operatons in the user interface, like showing the notebook
	index in the side pane, showing "what links here" and faster search
	for page attributes.

	The C{Index} object is an opaque object that only exposes an API
	to trigger index updates and notifications when changes are found.
	It does not expose the data it keeps directly. To query the index,
	you need to construct an "index view" first, see e.g. the
	L{PagesIndexView}, L{LinksIndexView} and L{TagsIndexView} classes.
	For convenience the L{Notebook} class also exposes these three
	views with the respective attributes C{pages}, C{links} and C{tags}.
	'''
	# We could also expose the above mentioned 3 views in this object,
	# but we don't do so to encourage the Notebook interface to be
	# used instead.

	@classmethod
	def new_from_file(klass, file, layout):
		'''Constructor for a file based index
		@param file: a L{File} object for the sqlite database
		@param layout: a L{NotebookLayout} instance to index
		'''
		file.dir.touch()
		db_conn = ThreadingDBConnection(file.encodedpath)
		return klass(db_conn, layout)

	@classmethod
	def new_from_memory(klass, layout):
		'''Constructor for an in-memory index
		@param layout: a L{NotebookLayout} instance to index
		'''
		db_conn = MemoryDBConnection()
		return klass(db_conn, layout)

	def __init__(self, db_conn, layout):
		'''Constructor
		@param db_conn: a L{DBConnection} object
		@param layout: a L{NotebookLayout} instance to index
		'''
		self.layout = layout
		self._indexers = [PagesIndexer(), LinksIndexer(), TagsIndexer()]
		self._pages = PagesViewInternal()
		self._index = IndexInternal(self.layout, self._indexers)
		self.db_conn = db_conn
		self._thread = None

		db_context = self.db_conn.db_change_context()

		try:
			with db_context as db:
				if self._index.get_property(db, 'db_version') != DB_VERSION:
					logger.debug('Index db_version out of date')
					self._db_init(db)
		except sqlite3.OperationalError:
			# db is there but table does not exist
			logger.debug('Operational error, init tabels')
			with db_context as db:
				self._db_init(db)
		except sqlite3.DatabaseError:
			if hasattr(db_conn, 'dbfilepath'):
				logger.warning('Overwriting possibly corrupt database: %s', db_conn.dbfilepath)
				db_conn.close_connections()
				file = File(self.db_conn.dbfilepath)
				if file.exists():
					file.remove()

				db_context = db_conn.db_change_context()
				with db_context as db:
					self._db_init(db)
			else:
				raise

		# TODO checks on locale, others?

		with db_context as db:
			db.execute('PRAGMA synchronous=OFF;')
			# Don't wait for disk writes, we can recover from crashes
			# anyway. Allows us to use commit more frequently.

	@property
	def probably_uptodate(self):
		with self.db_conn.db_context() as db:
			value = self._index.get_property(db, 'probably_uptodate')
			return False if value == '0' else True

	def _db_init(self, db):
		c = db.execute(
			'SELECT name FROM sqlite_master '
			'WHERE type="table" and name NOT LIKE "sqlite%"'
		)
		tables = [row[0] for row in c.fetchall()]
		for table in tables:
			db.execute('DROP TABLE %s' % table)

		logger.debug('(Re-)Initializing database for index')
		db.executescript(INDEX_INIT_SCRIPT)
		for indexer in self._indexers:
			indexer.on_db_init(self, db)

	def connect(self, signal, handler):
		for indexer in self._indexers:
			if signal in indexer.__signals__:
				return indexer.connect(signal, handler)
		else:
			raise ValueError, 'No such signal: %s' % signal

	def disconnect(self, handlerid):
		for indexer in self._indexers:
			indexer.disconnect(handlerid)
		# else pass

	def update(self, path=None):
		'''Update the index and return when done
		This method is faster than the background updates because
		it only commits the database at the end when all is done.
		@param path: a C{Path} object, if given only the index
		below this path is updated, else the entire index is updated.
		'''
		#~ print "--- Update ---"
		for i in self.update_iter(path):
			#~ print ">>", i
			continue

	def update_iter(self, path=None):
		self.stop_update()

		indexer = TreeIndexer.new_from_index(self)
		indexer.queue_check(path)

		for i in indexer:
			yield i

	def start_update(self, path=None):
		'''Start update in a separate thread.
		This is a relatively slow update because a separate commit is
		done for each page. The advantage is that changes become
		visible incrementally.
		If an update is
		'''
		indexer = TreeIndexer.new_from_index(self)
		indexer.queue_check(path)

		if not (self._thread and self._thread.is_alive()):
			self._thread = WorkerThread(indexer, "%s--%i" % (indexer.__class__.__name__, id(self.layout)))
			self._thread.start()

	def stop_update(self):
		'''Stop update thread if any'''
		if self._thread:
			self._thread.stop()
			self._thread = None

	def wait_for_update(self, timeout=None):
		'''Wait for update thread if any
		@param timeout: timeout in second
		@returns: C{True} is thread was still running at timeout, else
		C{False}
		'''
		if self._thread:
			self._thread.join(timeout)
			if self._thread.is_alive():
				return True # keep waiting
			else:
				self._thread = None
		return False

	def flush(self):
		'''Delete all data in the index'''
		logger.info('Flushing index')
		with self.db_conn.db_change_context() as db:
			self._db_init(db)

	def touch_current_page_placeholder(self, path):
		'''Create a placeholder for C{path} if the page does not
		exist. Cleans up old placeholders.
		'''
		# This method used a hack by linking the page from the ROOT_ID
		# page if it does not exist.

		with self.db_conn.db_change_context() as db:
			# delete
			db.execute(
				'DELETE FROM links WHERE source=?',
				(ROOT_ID,)
			)
			for indexer in self._indexers:
				if isinstance(indexer, LinksIndexer):
					indexer.cleanup_placeholders(self._index, db)

			# touch if needed
			try:
				indexpath = self._pages.lookup_by_pagename(db, path)
			except IndexNotFoundError:
				# insert link
				# insert placeholder
				target = self._index.touch_path(db, path)
				#~ self._index.set_page_exists(db, target, PAGE_EXISTS_HAS_CONTENT) # hack to avoid cleanup before next step :S
				db.execute(
					'INSERT INTO links(source, target, rel, names) '
					'VALUES (?, ?, ?, ?)',
					(ROOT_ID, target.id, HREF_REL_ABSOLUTE, target.name)
				)
				self._index.set_page_exists(db, target, PAGE_EXISTS_AS_LINK)
			else:
				pass # nothing to do

			self._index.before_commit(db)

		self._index.after_commit()

	def on_store_page(self, page):
		with self.db_conn.db_change_context() as db:
			try:
				indexpath = self._pages.lookup_by_pagename(db, page)
			except IndexNotFoundError:
				indexpath = self._index.touch_path(db, page)

			self._index.index_page(db, indexpath)
			self._index.update_parent(db, indexpath.parent)

			self._index.before_commit(db)

		self._index.after_commit()

	def on_move_page(self, oldpath, newpath):
		# TODO - optimize by letting indexers know about move
		if not (newpath == oldpath or newpath.ischild(oldpath)):
			self.on_delete_page(oldpath)
		self.update(newpath)

	def on_delete_page(self, path):
		with self.db_conn.db_change_context() as db:
			try:
				indexpath = self._pages.lookup_by_pagename(db, path)
			except IndexNotFoundError:
				return

			for child in self._pages.walk_bottomup(db, indexpath):
				self._index.delete_page(db, child, cleanup=False)

			last_deleted = self._index.delete_page(db, indexpath, cleanup=True)
			self._index.update_parent(db, last_deleted.parent)

			self._index.before_commit(db)

		self._index.after_commit()

	def add_plugin_indexer(self, indexer):
		'''Add an indexer for a plugin
		Checks the C{PLUGIN_NAME} and C{PLUGIN_DB_FORMAT}
		attributes and calls C{on_db_init()} when needed.
		Can result in reset of L{probably_uptodate} because the new
		indexer has not seen the pages in the index.
		@param indexer: An instantiation of L{PluginIndexerBase}
		'''
		assert indexer.PLUGIN_NAME and indexer.PLUGIN_DB_FORMAT
		with self.db_conn.db_change_context() as db:
			if self._index.get_property(db, indexer.PLUGIN_NAME) != indexer.PLUGIN_DB_FORMAT:
				indexer.on_db_init(self._index, db)
				self._index.set_property(db, indexer.PLUGIN_NAME, indexer.PLUGIN_DB_FORMAT)
				self._flag_reindex(db)

		self._indexers.append(indexer)

	def remove_plugin_indexer(self, indexer):
		'''Remove an indexer for a plugin
		Calls the C{on_teardown()} method of the indexer and
		remove it from the list.
		@param indexer: An instantiation of L{PluginIndexerBase}
		'''
		try:
			self._indexers.remove(indexer)
		except ValueError:
			pass

		with self.db_conn.db_change_context() as db:
			indexer.on_teardown(self._index, db)
			self._index.set_property(db, indexer.PLUGIN_NAME, None)

	def flag_reindex(self):
		'''This methods flags all pages with content to be re-indexed.
		Main reason to use this would be when loading a new plugin that
		wants to index all pages.
		'''
		with self.db_conn.db_change_context() as db:
			self._flag_reindex(db)

	def _flag_reindex(self, db):
		self._index.set_property(db, 'probably_uptodate', False)
		db.execute(
			'UPDATE pages SET content_etag=?, needscheck=? WHERE content_etag IS NOT NULL',
			('_reindex_', INDEX_CHECK_PAGE),
		)


class IndexInternal(object):
	'''Common methods between L{TreeIndexer} and L{Index}'''

	def __init__(self, layout, indexers):
		self.layout = layout
		self.indexers = indexers
		self._pages = PagesViewInternal()

	def before_commit(self, db):
		# This goes here to avoid link updates in between sequences
		# of multiple delete pages. That scenario goes wrong when there
		# are cross-refs because signals for delete and touch placeholder
		# will go out of sync.
		#
		# This is really a hack, but OK
		for indexer in self.indexers:
			if isinstance(indexer, LinksIndexer):
				indexer.check_links(self, db)

	def after_commit(self):
		# Callback for change context - emits SIGNAL_AFTER signals
		for indexer in self.indexers:
			indexer.emit_queued_signals()

	def get_property(self, db, key):
		c = db.execute('SELECT value FROM zim_index WHERE key=?', (key,))
		row = c.fetchone()
		if row:
			return row[0]
		else:
			return None

	def set_property(self, db, key, value):
		db.execute('DELETE FROM zim_index WHERE key=?', (key,))
		if key is not None:
			db.execute('INSERT INTO zim_index(key, value) VALUES (?, ?)', (key, value))

	def insert_page(self, db, parent, path, needscheck=INDEX_CHECK_PAGE):
		'''Insert a record for the page, but page does not really exists
		untill L{set_page_exists()} has been called.
		'''
		db.execute(
			'INSERT INTO pages(parent, basename, sortkey, needscheck) '
			'VALUES (?, ?, ?, ?)',
			(parent.id, path.basename, natural_sort_key(path.basename), needscheck)
		)
		indexpath = self._pages.lookup_by_parent(db, parent, path.basename)
		return indexpath

	def set_page_exists(self, db, indexpath, page_exists=PAGE_EXISTS_HAS_CONTENT):
		assert page_exists in (PAGE_EXISTS_AS_LINK, PAGE_EXISTS_HAS_CONTENT)

		for parent in reversed(list(indexpath.parents())): # top down
			parentrow = self._pages.lookup_by_indexpath(db, parent)
			if parentrow.page_exists < page_exists:
				self._set_page_exists(db, parentrow, page_exists)

		self._set_page_exists(db, indexpath, page_exists)

	def _set_page_exists(self, db, indexpath, page_exists):
		new = indexpath.page_exists == PAGE_EXISTS_UNCERTAIN
		db.execute(
			'UPDATE pages SET page_exists=? WHERE id=?',
			(page_exists, indexpath.id),
		)
		if new and not indexpath.isroot:
			for indexer in self.indexers:
				indexer.on_new_page(self, db, indexpath)

	def touch_path(self, db, path):
		parent = ROOT_PATH
		names = path.parts
		while names: # find existing parents
			try:
				indexpath = self._pages.lookup_by_parent(db, parent, names[0])
			except IndexNotFoundError:
				break
			else:
				names.pop(0)
				parent = indexpath

		while names: # create missing parts
			basename = names.pop(0)
			path = parent.child(basename)
			indexpath = self.insert_page(db, parent, path, needscheck=INDEX_UPTODATE)
			parent = indexpath

		return indexpath

	def index_page(self, db, indexpath):
		# Get etag first - when data changes these should
		# always be older to ensure changes are detected in next run
		assert isinstance(indexpath, IndexPathRow)
		file, folder = self.layout.map_page(indexpath)

		try:
			etag = str(file.mtime())
			ctime = datetime.fromtimestamp(file.ctime())
			mtime = datetime.fromtimestamp(file.mtime())

			if indexpath.page_exists != PAGE_EXISTS_HAS_CONTENT:
				self.set_page_exists(db, indexpath)

			format = self.layout.get_format(file)
			parsetree = format.Parser().parse(file.read())
			for indexer in self.indexers:
				indexer.on_index_page(self, db, indexpath, parsetree)

		except FileNotFoundError:
			etag = None
			ctime = None
			mtime = None
			for indexer in self.indexers:
				indexer.on_index_page(self, db, indexpath, None)

		db.execute(
			'UPDATE pages '
			'SET content_etag=?, ctime=?, mtime=? '
			'WHERE id=?',
			(etag, ctime, mtime, indexpath.id)
		)

	def delete_page(self, db, indexpath, cleanup):
		assert not indexpath.isroot

		if indexpath.n_children > 0 \
		and not all(row['page_exists'] == PAGE_EXISTS_AS_LINK
			for row in db.execute(
				'SELECT page_exists FROM pages WHERE parent=?',
				(indexpath.id,),
			)
		):
			raise AssertionError, 'Can not delete path with children'

		for indexer in self.indexers:
			indexer.on_delete_page(self, db, indexpath)

		if indexpath.n_children > 0:
			db.execute(
				'UPDATE pages SET page_exists=?, content_etag=?, ctime=?, mtime=?, children_etag=? WHERE id=?',
				(PAGE_EXISTS_AS_LINK, None, None, None, None, indexpath.id)
			)
		else:
			db.execute('DELETE FROM pages WHERE id=?', (indexpath.id,))

		parent = indexpath.parent
		basename = indexpath.basename
		for indexer in self.indexers:
			indexer.on_deleted_page(self, db, parent, basename)

		if cleanup and not indexpath.parent.isroot:
			# be careful, parent may already have disappeared due to
			# e.g. placeholder cleanup
			try:
				parent = self._pages.lookup_by_pagename(db, indexpath.parent)
			except IndexNotFoundError:
				pass
			else:
				if not self.check_existance(db, parent):
					return self.delete_page(db, parent, cleanup=True) # recurs

		# else
		return indexpath

	def check_existance(self, db, indexpath):
		if indexpath.hascontent:
			return True
		else:
			c = db.execute(
				'SELECT count(*) FROM pages '
				'WHERE parent=? and page_exists=?',
				(indexpath.id, PAGE_EXISTS_HAS_CONTENT)
			)
			return c.fetchone()[0] > 0

	def update_parent(self, db, parent):
		# To be called after inserting or deleting a page driven by
		# the notebook API (not driven by the indexer)

		# Get etag first - when data changes these should
		# always be older to ensure changes are detected in next run
		file, folder = self.layout.map_page(parent)
		etag = str(folder.mtime()) if folder.exists() else None
		if self.check_pagelist(db, parent):
			db.execute(
				'UPDATE pages SET children_etag=? WHERE id=?',
				(etag, parent.id)
			)
			# do not set 'needscheck', allow for recursive update in action
		else:
			pass # TODO - actively start indexer

	def check_pagelist(self, db, indexpath):
		# TODO - how to speed this up?
		names = set()
		for pagename in self.layout.index_list_children(indexpath):
			names.add(pagename.basename)

		try:
			for row in db.execute(
				'SELECT basename FROM pages WHERE parent=? and page_exists<>?',
				(indexpath.id, PAGE_EXISTS_AS_LINK)
			):
				names.remove(row['basename'])
		except KeyError:
			return False

		return not names # OK if empty


class TreeIndexer(IndexInternal):
	'''This indexer looks at the database for pages that are flagged
	as needing a check. It checks and where necessary updates the
	database cache.

	The C{__iter__()} function serves as the main loop for indexing
	all pages that are flagged. Thus a C{TreeIndexer} object can be
	used as iterable, e.g. in combination with the L{WorkerThread}
	class.
	'''

	@classmethod
	def new_from_index(klass, index):
		return klass(
			index.db_conn,
			index.layout,
			index._indexers
		)

	def __init__(self, db_conn, layout, indexers):
		self.db_conn = db_conn
		self.layout = layout
		self.indexers = indexers
		self._pages = PagesViewInternal()

	def queue_check(self, path, check=INDEX_CHECK_TREE):
		with self.db_conn.db_change_context() as db:
			while path and not path.isroot:
				try:
					path = self._pages.lookup_by_pagename(db, path)
				except IndexNotFoundError:
					path = path.parent
				else:
					break
			else:
				path = ROOT_PATH

			db.execute(
				'UPDATE pages SET needscheck=? WHERE id=?',
				(INDEX_CHECK_TREE, path.id)
			)

	def __iter__(self):
		# Run with commit after each cycle
		change_context = self.db_conn.db_change_context()
		update_iter = self.do_update_iter(change_context._db)
		while True:
			with change_context:
				try:
					i = update_iter.next()
					self.before_commit(change_context._db)
				except StopIteration:
					break
				else:
					yield i

			self.after_commit()

	def do_update_iter(self, db):
		logger.info('Starting index update')
		while True:
			# Get next page to be checked from db
			row = db.execute(
				'SELECT * FROM pages WHERE needscheck > 0 '
				'ORDER BY needscheck, id LIMIT 1'
			).fetchone()
				# ORDER BY: parents always have lower "id" than children

			if row:
				check = row['needscheck']
				indexpath = self._pages.lookup_by_row(db, row)
			else:
				break # Stop thread, index up to date

			# Dispatch to the proper method
			try:
				if check == INDEX_CHECK_CHILDREN:
					self.check_children(db, indexpath)
				elif check == INDEX_CHECK_TREE:
					self.check_children(db, indexpath, checktree=True)
				elif check == INDEX_CHECK_PAGE:
					self.check_page(db, indexpath)
				else:
					raise AssertionError('BUG: Unknown update flag: %i' % check)
			except:
				# Avoid looping for same page
				logger.exception('Error while handling update for page: %s', indexpath)
				db.execute(
					'UPDATE pages SET needscheck=? WHERE id=?',
					(INDEX_UPTODATE, indexpath.id)
				)

			# Let outside world know what we are doing
			# and allow wrapper to commit changes
			yield check, indexpath

		self.set_property(db, 'probably_uptodate', True)

		logger.info('Index update finished')

	def check_children(self, db, indexpath, checktree=False):
		### TODO check page_exists tag is correct here
		###      if not, propagate changes upward

		# Get etag first - when data changes these should
		# always be older to ensure changes are detected in next run
		file, folder = self.layout.map_page(indexpath)
		etag = str(folder.mtime()) if folder.exists() else None

		if etag != indexpath.children_etag:
			self.set_property(db, 'probably_uptodate', False)
			if etag and indexpath.n_children == 0:
				self.new_children(db, indexpath, etag)
			elif etag:
				self.update_children(db, indexpath, etag, checktree=checktree)
			else:
				self.delete_children(db, indexpath)
		elif checktree:
			# Check whether any grand-children changed
			# For a file store this may affect the children_etag
			# because creating the folder changes the parent folder
			# for memory store and other file layouts this behavior
			# differs.
			for pagename in self.layout.index_list_children(indexpath):
				row = db.execute(
					'SELECT * FROM pages WHERE parent=? and basename=?',
					(indexpath.id, pagename.basename)
				).fetchone()
				if row:
					file, folder = self.layout.map_page(pagename)
					if folder.exists() or row['n_children'] > 0: # has and/or had children
						check = INDEX_CHECK_TREE
					else:
						check = INDEX_CHECK_PAGE

					db.execute(
						'UPDATE pages SET needscheck=? WHERE id=?',
						(check, row['id'],)
					)
				else:
					raise IndexConsistencyError, 'Missing index for: %s' % pagename
		else:
			pass

		if checktree and not indexpath.isroot:
			needscheck = INDEX_CHECK_PAGE
		else:
			needscheck = INDEX_UPTODATE

		db.execute(
			'UPDATE pages SET children_etag=?, needscheck=? WHERE id=?',
			(etag, needscheck, indexpath.id)
		)

	def new_children(self, db, indexpath, etag):
		assert indexpath.n_children == 0
		for child_path in self.layout.index_list_children(indexpath):
			file, folder = self.layout.map_page(child_path)
			check = INDEX_CHECK_TREE if folder.exists() else INDEX_CHECK_PAGE
			child = self.insert_page(db, indexpath, child_path, needscheck=check)
			if file and file.exists():
				self.set_page_exists(db, child)

	def update_children(self, db, indexpath, etag, checktree=False):
		c = db.cursor()

		# First flag all children in index
		c.execute('UPDATE pages SET childseen=0 WHERE parent=? and page_exists<>?',
			(indexpath.id, PAGE_EXISTS_AS_LINK)
		)

		# Then go over the list
		for child_path in self.layout.index_list_children(indexpath):
			file, folder = self.layout.map_page(child_path)
			c.execute(
				'SELECT * FROM pages WHERE parent=? and basename=?',
				(indexpath.id, child_path.basename)
			)
			row = c.fetchone()
			if not row: # New child
				check = INDEX_CHECK_TREE if folder.exists() else INDEX_CHECK_PAGE
				child = self.insert_page(db, indexpath, child_path, needscheck=check)
				if file and file.exists():
					self.set_page_exists(db, child)
			else: # Existing child
				if file and file.exists() and row['page_exists'] != PAGE_EXISTS_HAS_CONTENT:
					child = self._pages.lookup_by_row(db, row)
					self.set_page_exists(db, child)

				if checktree:
					if folder.exists() or row['n_children'] > 0: # has and/or had children
						check = INDEX_CHECK_TREE
					else:
						check = INDEX_CHECK_PAGE
				else:
					if file and file.exists() != bool(row['content_etag']):
						check = INDEX_CHECK_PAGE
					elif page.haschildren != (row['n_children'] > 0):
						check = INDEX_CHECK_CHILDREN
					else:
						check = None

				if check is None:
					c.execute(
						'UPDATE pages SET childseen=1 WHERE id=?',
						(row['id'],)
					)
				else:
					c.execute(
						'UPDATE pages SET childseen=1, needscheck=? WHERE id=?',
						(check, row['id'],)
					)

		# Finish by deleting pages that went missing
		for row in c.execute(
			'SELECT * FROM pages WHERE parent=? and childseen=0',
			(indexpath.id,)
		):
			child = self._pages.lookup_by_row(db, row)
			self.delete_children(db, child)
			self.delete_page(db, child, cleanup=False)

	def delete_children(self, db, indexpath):
		for row in db.execute(
			'SELECT * FROM pages WHERE parent=?',
			(indexpath.id,)
		):
			child = indexpath.child_by_row(row)
			self.delete_children(db, child) # recurs depth first - no check here on haschildren!
			try:
				child = self._pages.lookup_by_indexpath(db, child)
			except IndexConsistencyError:
				pass # page cleanup already in the process
			else:
				self.delete_page(db, child, cleanup=False)

	def check_page(self, db, indexpath):
		file, folder = self.layout.map_page(indexpath)
		etag = str(file.mtime()) if file.exists() else None
		if etag != indexpath.content_etag:
			self.index_page(db, indexpath)

		# Queue a children check if needed (not recursive)
		children_etag = str(folder.mtime()) if folder.exists() else None
		if children_etag == indexpath.children_etag:
			needscheck = INDEX_UPTODATE
		else:
			self.set_property(db, 'probably_uptodate', False)
			needscheck = INDEX_CHECK_CHILDREN
		db.execute(
			'UPDATE pages SET needscheck=? WHERE id=?',
			(needscheck, indexpath.id)
		)


class DBConnection(object):
	'''A DBConnection object manages one or more connections to the
	same database.

	Database access is protected by two locks: a "state lock" and
	a "change lock". The state lock is used by all objects that want
	to retrieve info from the index. As long as you hold the lock,
	nobody is going to change the index in between. However, changes can
	be pending to be committed when you release the lock. Aquire the
	change lock to ensure nobody is planning changes in paralel.

	This logic is enforced by wrapping the database connections in
	L{DBContext} and L{DBChangeContext} objects. The first is used by
	objects that only want a view of the database, the second by the
	index when changing the database.

	Do not instantiate this class directly, use implementations
	L{ThreadingDBConnection} or L{MemoryDBConnection} instead.
	'''

	def __init__(self):
		raise NotImplementedError

	@staticmethod
	def _db_connect(string):
		# We use the undocumented "check_same_thread=False" argument to
		# allow calling database from multiple threads. This allows
		# views to be used from different threads as well. The state lock
		# protects the access to the connection in that case.
		# For threads that make changes, a new connection is made anyway
		db = sqlite3.connect(
			string,
			detect_types=sqlite3.PARSE_DECLTYPES,
			check_same_thread=False,
		)
		db.row_factory = sqlite3.Row
		return db

	def db_context(self):
		'''Returns a L{DBContext} object'''
		return DBContext(self._get_db(), self._state_lock)

	def db_change_context(self):
		'''Returns a L{DBChangeContext} object'''
		return DBChangeContext(
			self._get_db(),
			self._state_lock, self._change_lock
		)

	def close_connections(self):
		raise NotImplementedError


class MemoryDBConnection(DBConnection):

	def __init__(self):
		self._db = self._db_connect(':memory:')
		self._state_lock = threading.RLock()
		self._change_lock = self._state_lock # all changes visile immediatly

	def _get_db(self):
		return self._db


class ThreadingDBConnection(DBConnection):
	'''Implementation of L{DBConnection} that re-connects for each
	thread. The advantage is that changes made in a thread do not
	become visible to other threads untill they are committed.
	'''

	def __init__(self, dbfilepath):
		if dbfilepath == ':memory:':
			raise ValueError, 'This class can not work with in-memory databases, use MemoryDBConnection instead'
		self.dbfilepath = dbfilepath
		self._connections = {}
		self._state_lock = threading.RLock()
		self._change_lock = threading.RLock()

	def _get_db(self):
		thread = threading.current_thread().ident
		if thread not in self._connections:
			self._connections[thread] = self._db_connect(self.dbfilepath)
		return self._connections[thread]

	def close_connections(self):
		for key in self._connections.keys():
			db = self._connections.pop(key)
			db.close()


class DBContext(object):
	'''Used for using a db connection with an asociated lock.
	Intended syntax::

		self._db = DBContext(db_conn, state_lock)
		...

		with self._db as db:
			db.execute(...)
	'''

	def __init__(self, db, state_lock):
		self._db = db
		self.state_lock = state_lock

	def __enter__(self):
		self.state_lock.acquire()
		self._total_changes = self._db.total_changes
		return self._db

	def __exit__(self, exc_type, exc_value, traceback):
		self.state_lock.release()
		assert self._total_changes == self._db.total_changes, 'Unexpected changes to db'
		return False # re-raise error


class DBChangeContext(object):
	'''Context manager to manage database changes.
	Intended syntax::

		self._db = DBChangeContext(db_conn, state_lock, change_lock)
		...

		with self._db as db:
			db.execute(...)
	'''

	def __init__(self, db, state_lock, change_lock):
		self._db = db
		self.state_lock = state_lock
		self.change_lock = change_lock
		self._counter = 0 # counter makes commit re-entrant

	def __enter__(self):
		self.change_lock.acquire()
		self._counter += 1
		return self._db

	def __exit__(self, exc_type, exc_value, traceback):
		try:
			self._counter -= 1
			if self._counter == 0:
				if exc_value:
					self._db.rollback()
				else:
					with self.state_lock:
						self._db.commit()
		finally:
			self.change_lock.release()

		return False # re-raise error
