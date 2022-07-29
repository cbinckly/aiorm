import setuptools

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setuptools.setup(
    name="aio-api-sm", # Replace with your own username
    version="1.0",
    author="cbinckly",
    author_email="cbinckly@gmail.com",
    packages=['aio_api_sm'],
    install_requires=[
        'python-dateutil',
        'aiohttp',
    ],
    description="Asynchronous HTTP Session Manager with retry, rate limiting, and more.",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://aio-api-sm.rtfd.io",
    classifiers=[
        "Programming Language :: Python :: 3",
        "Operating System :: OS Independent",
    ],
    python_requires='>=3.9',
)

