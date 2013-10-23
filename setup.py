from distutils.core import setup, Extension

setup(name="gsmws",
      version="0.0.1",
      description="GSMWS for OpenBTS",
      author="Shaddi Hasan",
      author_email="shaddi@cs.berkeley.edu",
      url="http://cs.berkeley.edu/~shaddi",
      license='bsd',
      packages=['gsmws'],
      scripts=['GSMWSControl'],
      #data_files=[('/etc/', ['conf/foo.conf']),
      classifiers=[
        'Operating System :: POSIX',
        'Development Status :: 4 - Beta',
        'License :: OSI Approved :: BSD License',
        'Programming Language :: Python :: 2.6',
        'Topic :: Communications :: Telephony',
        'Topic :: Utilities',],
      keywords='gsm openbts vbts',
)
