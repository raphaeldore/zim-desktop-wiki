# -*- coding: utf-8 -*-

# Copyright 2009 Jaap Karssenberg <jaap.karssenberg@gmail.com>

import tests

from subprocess import check_call

from zim.fs import _md5, File, Dir
from zim.config import data_file
from zim.notebook import Path, Notebook, init_notebook
from zim.exporter import Exporter, StaticLinker

# TODO add check that attachments are copied correctly


def md5(f):
	return _md5(f.raw())


class TestLinker(tests.TestCase):

	def runTest(self):
		'''Test proper linking of files in export'''
		notebook = tests.new_notebook(fakedir='/source/dir/')

		linker = StaticLinker('html', notebook)
		linker.set_usebase(True) # normally set by html format module
		linker.set_path(Path('foo:bar')) # normally set by exporter
		linker.set_base(Dir('/source/dir/foo')) # normally set by exporter

		self.assertEqual(linker.link_page('+dus'), './bar/dus.html')
		self.assertEqual(linker.link_page('dus'), './dus.html')
		self.assertEqual(linker.link_file('./dus.pdf'), './bar/dus.pdf')
		self.assertEqual(linker.link_file('../dus.pdf'), './dus.pdf')
		self.assertEqual(linker.link_file('../../dus.pdf'), '../dus.pdf')


@tests.slowTest
class TestExport(tests.TestCase):

	options = {'format': 'html', 'template': 'Default'}

	def setUp(self):
		self.dir = Dir(self.create_tmp_dir('exported_files'))

	def export(self):
		notebook = tests.new_notebook(fakedir='/foo/bar')

		exporter = Exporter(notebook, **self.options)
		exporter.export_all(self.dir)

	def runTest(self):
		'''Test export notebook to html'''
		self.export()

		file = self.dir.file('Test/foo.html')
		self.assertTrue(file.exists())
		text = file.read()
		self.assertTrue('<!-- Wiki content -->' in text, 'template used')
		self.assertTrue('<h1>Foo</h1>' in text)

		for icon in ('checked-box',): #'unchecked-box', 'xchecked-box'):
			# Default template doesn't have its own checkboxes
			self.assertTrue(self.dir.file('_resources/%s.png' % icon).exists())
			self.assertEqual(
				md5(self.dir.file('_resources/%s.png' % icon)),
				md5(data_file('pixmaps/%s.png' % icon))
			)


@tests.slowTest
class TestExportTemplateResources(TestExport):

	options = {
		'format': 'html',
		'template': './tests/data/templates/Default.html'
	}

	def runTest(self):
		'''Test export notebook to html with template resources'''
		self.export()

		file = self.dir.file('Test/foo.html')
		self.assertTrue(file.exists())
		text = file.read()
		self.assertTrue('src="../_resources/foo/bar.png"' in text)
		self.assertTrue(self.dir.file('_resources/foo/bar.png').exists())

		for icon in ('checked-box',): #'unchecked-box', 'xchecked-box'):
			# Template has its own checkboxes
			self.assertTrue(self.dir.file('_resources/%s.png' % icon).exists())
			self.assertNotEqual(
				md5(self.dir.file('_resources/%s.png' % icon)),
				md5(data_file('pixmaps/%s.png' % icon))
			)


class TestExportFullOptions(TestExport):

	options = {'format': 'html', 'template': 'Default',
			'index_page': 'index', 'document_root_url': 'http://foo.org/'}

	def runTest(self):
		'''Test export notebook to html with all options'''
		TestExport.runTest(self)
		file = self.dir.file('index.html')
		self.assertTrue(file.exists())
		indexcontent = file.read()
		self.assertTrue('<a href="./Test/foo.html" title="foo">foo</a>' in indexcontent)


class TestExportCommandLine(TestExportFullOptions):

	def export(self):
		dir = Dir(self.create_tmp_dir('source_files'))
		init_notebook(dir)
		notebook = Notebook(dir=dir)
		for name, text in tests.WikiTestData:
			page = notebook.get_page(Path(name))
			page.parse('wiki', text)
			notebook.store_page(page)
		file = dir.file('Test/foo.txt')
		self.assertTrue(file.exists())

		check_call(['python', './zim.py', '--export', '--template=Default', dir.path, '--output', self.dir.path, '--index-page', 'index'])

	def runTest(self):
		'''Test export notebook to html from commandline'''
		TestExportFullOptions.runTest(self)

# TODO test export single page from command line


@tests.slowTest
class TestExportDialog(tests.TestCase):

	def runTest(self):
		'''Test ExportDialog'''
		from zim.gui.exportdialog import ExportDialog

		dir = Dir(self.create_tmp_dir())

		notebook = tests.new_notebook(fakedir='/foo/bar')

		ui = tests.MockObject()
		ui.notebook = notebook
		ui.page = Path('foo')
		ui.mainwindow = None

		## Test export all pages
		dialog = ExportDialog(ui)
		dialog.set_page(0)

		page = dialog.get_page()
		page.form['selection'] = 'all'
		dialog.next_page()

		page = dialog.get_page()
		page.form['format'] = 'HTML'
		page.form['template'] = 'Print'
		dialog.next_page()

		page = dialog.get_page()
		page.form['folder'] = dir
		page.form['index'] = 'INDEX_PAGE'
		dialog.assert_response_ok()

		file = dir.file('Test/foo.html')
		self.assertTrue(file.exists())
		text = file.read()
		self.assertTrue('<!-- Wiki content -->' in text, 'template used')
		self.assertTrue('<h1>Foo</h1>' in text)


		## Test export single page
		dialog = ExportDialog(ui)
		dialog.set_page(0)

		page = dialog.get_page()
		page.form['selection'] = 'page'
		page.form['page'] = 'Test:foo'
		dialog.next_page()

		page = dialog.get_page()
		page.form['format'] = 'HTML'
		page.form['template'] = 'Print'
		dialog.next_page()

		page = dialog.get_page()
		page.form['file'] = dir.file('SINGLE_FILE_EXPORT.html').path
		dialog.assert_response_ok()

		file = dir.file('SINGLE_FILE_EXPORT.html')
		self.assertTrue(file.exists())
		text = file.read()
		self.assertTrue('<!-- Wiki content -->' in text, 'template used')
		self.assertTrue('<h1>Foo</h1>' in text)
