#!/bin/bash

htmldir="../html"

for file in `find . -type f -name "*.gmi"`
do
   barefile=`basename -s .gmi $file`
   outfile="$htmldir/$file"
   outdir=`dirname $outfile`
   mkdir -p $outdir
   outfile="$outdir/$barefile.html"

   title=$(cat $file|grep -m 1 '^# '|sed 's/#//'|awk '{$1=$1;print}')
   gem2html.awk $file | sed "s/TITLEEE/$title/" > $outfile
done

