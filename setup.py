import setuptools

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setuptools.setup(
    name="aiorm", # Replace with your own username
    version="1.0",
    author="cbinckly",
    author_email="cbinckly@gmail.com",
    packages=['aiorm'],
    install_requires=[
        'aiohttp',
    ],
    description="Asynchronous HTTP Request Manager.",
    long_description=long_description,
    long_description_content_type="text/md",
    url="https://aiorm.rtfd.io",
    classifiers=[
        "Programming Language :: Python :: 3",
        "Operating System :: OS Independent",
    ],
    python_requires='>=3.9',
)

